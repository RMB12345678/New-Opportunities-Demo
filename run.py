"""
Main entry point.

Run order (this is the "check before run" step you asked for):
  1. Pull every company from the Notion Target List database.
  2. Find any rows where ATS Platform is blank (e.g. a company you just
     added manually). For each one, auto-search for its ATS and write
     the result back to Notion BEFORE the scrape step runs, so newly
     added companies never get silently skipped.
  3. Re-pull the (now fully populated) company list.
  4. Bucket companies by Scrape Method: API-backed platforms get scraped
     automatically; everything else is listed separately for manual
     follow-up rather than silently dropped.
  5. Run the matching scraper for every API-backed company and write
     all results to output/jobs.json.

Usage:
    python run.py
    python run.py --dry-run   # skip every Notion write and Anthropic call;
                               # print what the run would have done instead
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import argparse
import json
import os
import re
import time
from contextlib import contextmanager
from urllib.parse import quote_plus, unquote

from dotenv import load_dotenv
load_dotenv()  # reads the .env file in this folder and loads NOTION_API_KEY, etc.

from notion.client import (
    get_all_companies, get_companies_missing_ats, get_companies_missing_profile,
    get_companies_missing_url, get_companies_missing_website,
    update_ats_fields, update_company_info, update_company_website,
    sync_jobs_to_notion, sync_scrape_status,
    SCRAPE_OK, SCRAPE_FAILED, SCRAPE_NO_SCRAPER, SCRAPE_NO_URL,
    api_call_report,
)
from ats_finder.find_ats import find_ats, find_company_info, find_via_search
from ats_finder.find_website import derive_website
from scrapers import greenhouse, lever, ashby, workday, html_generic
from scoring.score_jobs import score_all, RUBRIC_FINGERPRINT
from scoring.track_new_postings import mark_new_postings
from output_excel import build_workbook

# --- step timing ------------------------------------------------------
# Wall clock per pipeline step, printed as a table at the end of the run.
# This exists because "the run got slow" is not a diagnosis: with nine
# steps, three of which talk to Notion and one of which talks to 236
# websites, the cost could plausibly sit in any of them, and the obvious
# suspect (scoring) turned out to be the cheapest. Measure, then cut.
#
# perf_counter rather than time.time: monotonic, so a clock adjustment
# mid-run can't produce a negative step.
_STEP_TIMINGS = []


@contextmanager
def timed(label):
    start = time.perf_counter()
    try:
        yield
    finally:
        _STEP_TIMINGS.append((label, time.perf_counter() - start))


def print_timing_report():
    if not _STEP_TIMINGS:
        return
    total = sum(seconds for _, seconds in _STEP_TIMINGS)
    width = max(len(label) for label, _ in _STEP_TIMINGS)
    print("\n=== Timing (wall clock per step) ===")
    for label, seconds in _STEP_TIMINGS:
        share = (100.0 * seconds / total) if total else 0.0
        print(f"  {label:<{width}}  {seconds:8.1f}s  {share:5.1f}%")
    print(f"  {'TOTAL':<{width}}  {total:8.1f}s")


def print_api_report(dry_run=False):
    """Notion request volume for the run, split reads vs. writes. A read
    pages 100 rows at a time; a write is one row plus a throttle sleep, so
    the write count is what actually sets the wall clock of the sync.

    Under --dry-run these are the writes a real run WOULD have made: the
    counters deliberately sit before the dry-run short-circuit in each
    helper, because a rehearsal that reported zero writes would answer the
    one question it exists to answer with a number that is always zero."""
    report = api_call_report()
    label = "planned Notion API calls (dry run — nothing was written)" if dry_run         else "Notion API calls"
    print(f"\n=== {label} ===")
    for kind, count in sorted(report["reads"].items()):
        print(f"  {kind:<28} {count:6}")
    print(f"  {'READS (total)':<28} {report['read_total']:6}")
    for kind, count in sorted(report["writes"].items()):
        print(f"  {kind:<28} {count:6}")
    print(f"  {'WRITES (total)':<28} {report['write_total']:6}")
    print(f"  {'~throttle cost of writes':<28} {report['write_total'] * 0.35:6.0f}s")


SCRAPERS = {
    "Greenhouse": greenhouse.fetch_jobs,
    "Lever": lever.fetch_jobs,
    "Ashby": ashby.fetch_jobs,
    "Workday": workday.fetch_jobs,
}


def fill_missing_profile_fields(dry_run=False):
    """Catch-up pass: companies that already have an ATS but are still
    missing Sector or HQ (e.g. added before this fix existed, or hit an
    earlier bug where the fast-path skipped research). Looks up a full
    profile via search but only fills the fields that are actually
    blank — never overwrites an existing ATS Platform value.

    Writes a persistent "Not found" marker into any field that's still
    empty after a genuine search attempt, and a persistent "AMBIGUOUS"
    marker for name collisions. Without this, an empty result looks
    identical to "never searched," so the exact same company gets
    re-searched (and re-paid for) on every single future run, forever.

    dry_run=True skips find_via_search (the Anthropic call) entirely —
    it just lists which companies would have been researched.
    """
    missing = get_companies_missing_profile()
    if not missing:
        print("[check] Every company already has sector/HQ filled in. Continuing.")
        return

    if dry_run:
        print(f"[check] [dry-run] {len(missing)} company(ies) missing sector/HQ "
              f"would be researched via Anthropic:")
        for row in missing:
            print(f"  -> {row['company']}")
        return

    NOT_FOUND = "Not found (checked, no info located — clear this field to retry)"

    print(f"[check] {len(missing)} company(ies) missing sector/HQ. Backfilling now...")
    for row in missing:
        print(f"  -> {row['company']}")
        try:
            info = find_via_search(row["company"], hq=row.get("hq"))
        except Exception as e:
            print(f"     search failed: {e}")
            continue

        if info["ambiguous"]:
            print(f"     [AMBIGUOUS] {info['ambiguity_note']}")
            update_company_info(
                row["page_id"],
                sector=f"AMBIGUOUS — {info['ambiguity_note']}" if not row.get("sector") else None,
                hq="AMBIGUOUS — see Sector / Focus for details" if not row.get("hq") else None,
            )
            continue

        sector_value = info["sector"] or NOT_FOUND
        hq_value = info["hq"] or NOT_FOUND

        update_company_info(
            row["page_id"],
            sector=sector_value if not row.get("sector") else None,
            hq=hq_value if not row.get("hq") else None,
            careers_url=info["careers_url"] if not row.get("careers_url") else None,
            # deliberately NOT touching ats_platform/scrape_method here —
            # this pass only backfills sector/HQ, it doesn't second-guess
            # an ATS that was already confirmed.
        )
        print(f"     filled: {sector_value} | {hq_value}")
        time.sleep(1)


def fill_missing_ats(dry_run=False):
    """For any company with a blank ATS Platform (the signal that it's a
    newly added, not-yet-researched company), look up its FULL profile —
    sector, HQ, ATS, careers URL — in one pass and write it all back.

    If the name is ambiguous (matches multiple real companies), this does
    NOT guess. It writes a clear "AMBIGUOUS" marker into ATS Platform (so
    the next run doesn't keep re-searching and re-spending on it) along
    with a note describing the candidates, and prints a loud warning so
    you know to go add a disambiguating detail in Notion — e.g. append
    the industry or HQ to the company name, or fill in HQ yourself — then
    clear ATS Platform back to blank to trigger a fresh, disambiguated
    lookup next run.

    dry_run=True skips find_company_info (the Anthropic call) entirely —
    it just lists which companies would have been researched.
    """
    missing = get_companies_missing_ats()
    if not missing:
        print("[check] Every company already has an ATS value. Continuing.")
        return

    if dry_run:
        print(f"[check] [dry-run] {len(missing)} company(ies) missing ATS data "
              f"would be researched via Anthropic:")
        for row in missing:
            print(f"  -> {row['company']}")
        return

    print(f"[check] {len(missing)} company(ies) missing ATS data. Searching now...")
    for row in missing:
        print(f"  -> {row['company']}")
        try:
            info = find_company_info(row["company"], hq=row.get("hq"))
        except Exception as e:
            # An exception here means the SEARCH ITSELF failed (network
            # error, API outage, billing issue) — NOT that we searched and
            # confirmed nothing exists. Those are different situations and
            # must be treated differently: writing a "not found" marker here
            # would permanently mark this company as unsearchable based on
            # a transient failure, defeating the whole point of the
            # retry-safety fix. Skip and leave it blank so a future run
            # (once whatever's wrong is fixed) retries it properly.
            print(f"     search failed (not a real answer, will retry next run): {e}")
            continue

        if info["ambiguous"]:
            print(f"     [AMBIGUOUS] {info['ambiguity_note']}")
            print(f"     -> Add a disambiguating detail to '{row['company']}' in Notion "
                  f"(e.g. industry, HQ, or a more specific name), then clear its ATS "
                  f"Platform field to blank to re-trigger a fresh lookup.")
            update_company_info(
                row["page_id"],
                sector="AMBIGUOUS — see console output from this run for candidates",
                hq="AMBIGUOUS — see note",
                ats_platform="AMBIGUOUS — needs disambiguation (see Sector / Focus)",
                scrape_method="Manual check",
            )
            continue

        update_company_info(
            row["page_id"],
            sector=info["sector"] if not row.get("sector") else None,  # don't overwrite existing
            hq=info["hq"] if not row.get("hq") else None,
            source=None if row.get("source") else "Added by user (auto-filled)",
            ats_platform=info["ats_platform"],
            scrape_method=info["scrape_method"],
            careers_url=info["careers_url"],
        )
        print(f"     found: {info['ats_platform']} / {info['scrape_method']}"
              + (f" / {info['careers_url']}" if info["careers_url"] else "")
              + (f" | {info['sector']}" if info["sector"] else "")
              + (f" | HQ: {info['hq']}" if info["hq"] else ""))
        time.sleep(1)  # be polite to Notion's rate limit


def google_search_fallback_url(company_name):
    """A real, clickable URL to use only when NEITHER a dedicated careers
    page NOR a homepage could be found by search — the true last resort.
    A pre-filled Google search is still genuinely useful (one click gets
    you searching) and, being non-blank, prevents this company from being
    re-searched on every future run.
    """
    query = quote_plus(f"{company_name} careers")
    return f"https://www.google.com/search?q={query}"


def fill_missing_careers_urls(dry_run=False):
    """Any company missing a saved Careers URL — regardless of whether it's
    API-tier or HTML-scrape — gets one looked up now. This used to only
    check HTML-scrape companies, which silently starved the API scrapers
    of the real, confirmed URLs they need (see get_companies_missing_url's
    docstring for the full story on why that mattered).

    Fallback chain: dedicated careers page -> company homepage -> a
    pre-filled Google search as the true last resort. Every path writes a
    real, non-blank URL, so a company is never re-searched (and re-paid
    for) once it's been looked at once — Notion's own "already has a URL"
    check is what prevents that going forward, no separate tracking needed.

    dry_run=True skips find_company_info (the Anthropic call) entirely —
    it just lists which companies would have been researched.
    """
    missing = get_companies_missing_url()
    if not missing:
        print("[check] Every company already has a Careers URL. Continuing.")
        return

    if dry_run:
        print(f"[check] [dry-run] {len(missing)} company(ies) missing a Careers URL "
              f"would be researched via Anthropic:")
        for row in missing:
            print(f"  -> {row['company']}")
        return

    print(f"[check] {len(missing)} company(ies) missing a Careers URL. Searching now...")
    for row in missing:
        print(f"  -> {row['company']}")

        # If this company's ATS is already a confirmed API platform
        # (Greenhouse/Lever/Ashby), target the search at THAT platform's
        # specific URL instead of a generic careers-page search — this is
        # what stops the search from saving a marketing homepage that the
        # scraper then can't extract a slug from.
        _, known_platform = resolve_scraper(row["ats_platform"])

        try:
            info = find_company_info(row["company"], hq=row.get("hq"), known_platform=known_platform)
        except Exception as e:
            # Same distinction as fill_missing_ats(): a search that FAILED
            # to run is not the same as a search that ran and found
            # nothing. Skip without writing anything, so this gets a real
            # attempt next run instead of a permanent false "not found".
            print(f"     search failed (not a real answer, will retry next run): {e}")
            continue

        url = info.get("careers_url")

        if url and info.get("used_homepage_fallback"):
            update_ats_fields(row["page_id"], row["ats_platform"], row["scrape_method"], careers_url=url)
            print(f"     no dedicated careers page found — using homepage as fallback: {url}")
        elif url:
            update_ats_fields(row["page_id"], row["ats_platform"], row["scrape_method"], careers_url=url)
            print(f"     found: {url}")
        else:
            # Neither a dedicated careers page nor a homepage was found —
            # true last resort: a pre-filled Google search. Still clickable
            # and genuinely useful, and being non-blank, this also removes
            # the need to track a separate local "dead ends" list, since
            # the field itself is no longer blank on future runs.
            fallback_url = google_search_fallback_url(row["company"])
            update_ats_fields(row["page_id"], row["ats_platform"], row["scrape_method"], careers_url=fallback_url)
            print(f"     no careers page or homepage found — using Google search fallback: {fallback_url}")
        time.sleep(1)


def fill_missing_website(dry_run=False):
    """Backfill each company's Website property, and stamp its page icon
    with a real logo (or a blank marker) either way — see
    ats_finder/find_website.py for the derivation logic and
    get_companies_missing_website() in notion/client.py for why the icon,
    not just the Website value, is what marks a row as already checked.

    Deliberately not folded into fill_missing_careers_urls(): that pass
    runs BEFORE this one so its own backfilled Careers URLs are already in
    Notion by the time derive_website() looks at them — a company whose
    careers page was just discovered this run can still resolve its
    Website for free, from the URL fill_missing_careers_urls() just wrote,
    instead of paying for a second, separate search.

    dry_run=True skips every lookup (local or Anthropic) and every write —
    it just lists which companies would be checked, same contract as the
    other fill_missing_* passes above.
    """
    missing = get_companies_missing_website()
    if not missing:
        print("[check] Every company already has a Website (or was already checked). Continuing.")
        return

    if dry_run:
        print(f"[check] [dry-run] {len(missing)} company(ies) missing a Website would be "
              f"checked (free extraction from their Careers URL first, Anthropic search "
              f"only if that comes up empty):")
        for row in missing:
            print(f"  -> {row['company']}")
        return

    print(f"[check] {len(missing)} company(ies) missing a Website. Deriving now...")
    derived, blanked, skipped = 0, 0, 0
    for row in missing:
        try:
            website, icon = derive_website(row.get("careers_url"), row["company"], hq=row.get("hq"))
        except Exception as e:
            # A failed LOOKUP (network/API outage) is not a genuine "nothing
            # found" — same distinction fill_missing_ats() makes. Leave the
            # row untouched (no icon stamped) so it gets a real attempt next
            # run instead of being permanently marked blank.
            print(f"  -> {row['company']}: lookup failed (not a real answer, will retry next run): {e}")
            skipped += 1
            continue

        if website:
            print(f"  -> {row['company']}: {website}")
            derived += 1
        else:
            print(f"  -> {row['company']}: no website found — stamping blank icon")
            blanked += 1

        update_company_website(row["page_id"], website=website, icon=icon)
        time.sleep(1)  # be polite to Notion's rate limit

    print(f"[check] {derived} website(s) derived, {blanked} stamped blank (nothing found), "
          f"{skipped} left for retry next run")


def resolve_scraper(platform_raw):
    """Find a matching scraper for a company's ATS Platform text field.

    Does NOT require an exact match — this field often has extra context
    attached (e.g. "Greenhouse (acquired by Getinge)", trailing whitespace
    from a Notion import), so this searches for a known platform name
    anywhere in the text, case-insensitive, on a whole-word basis.

    Returns (scraper_function, matched_name) or (None, None).
    If more than one known platform name appears in the text (e.g. an
    "X and/or Y (ambiguous)" note), the first match is used but a warning
    is printed, since that company's ATS was left genuinely uncertain.
    """
    if not platform_raw:
        return None, None

    matches = []
    for name, fn in SCRAPERS.items():
        if re.search(rf"\b{re.escape(name)}\b", platform_raw, re.IGNORECASE):
            matches.append((name, fn))

    if not matches:
        return None, None
    if len(matches) > 1:
        print(f"  [warn] ambiguous ATS field ('{platform_raw}') matched multiple "
              f"platforms {[m[0] for m in matches]} — using {matches[0][0]}, verify manually")
    return matches[0][1], matches[0][0]


# URL path patterns for extracting a confirmed slug from a saved Careers
# URL, keyed by the same platform names used in SCRAPERS. This is what lets
# the scraper use the REAL, verified slug (e.g. "adaptive-innovations")
# instead of re-guessing one from the company name (e.g. "adaptiveinnovations",
# missing the hyphen) — guessing is why previously-confirmed companies like
# Adaptive Innovations, CMR Surgical, and Capstan Medical were failing with
# 404s every run even though we already knew their real careers URL.
SLUG_URL_PATTERNS = {
    "Greenhouse": [
        r"job-boards\.greenhouse\.io/([a-zA-Z0-9\-_]+)",
        r"boards\.greenhouse\.io/([a-zA-Z0-9\-_]+)",
        r"boards-api\.greenhouse\.io/v1/boards/([a-zA-Z0-9\-_]+)",
    ],
    "Lever": [r"jobs\.lever\.co/([a-zA-Z0-9\-_]+)"],
    "Ashby": [r"jobs\.ashbyhq\.com/([^/?#]+)"],
}


def extract_slug(careers_url, platform_name):
    """Pull the real ATS slug out of a saved Careers URL, if possible."""
    if not careers_url or platform_name not in SLUG_URL_PATTERNS:
        return None
    for pattern in SLUG_URL_PATTERNS[platform_name]:
        m = re.search(pattern, careers_url)
        if m:
            return unquote(m.group(1))
    return None


def extract_workday_params(careers_url):
    """Workday's answer to extract_slug().

    The single-slug platforms above are identified by one token, so
    extract_slug() can return a string. A Workday board takes four pieces
    (host, wd number, tenant, site) plus an optional facet filter for the
    shared tenants, so it needs its own resolver rather than a wider
    contract on extract_slug() that the other three would have to ignore.

    Returns None when the saved URL is not a myworkdayjobs.com board — a
    marketing careers page, say. That None is deliberately NOT a soft
    failure that falls through to the generic HTML scraper: HTML is what
    produced the false zeros for every Workday company in the first place,
    so an unparseable URL is flagged for a human instead.
    """
    return workday.parse_board_url(careers_url)


def run_scrapers(companies):
    """Scrape every company and return (all_jobs, scraped_ok, statuses).

    scraped_ok is the set of company names whose scraper returned without
    raising. It exists so sync_jobs_to_notion() can tell "this company's
    postings are genuinely gone" apart from "we never got a usable answer
    about this company at all". Without that distinction, a timeout or a
    transient 500 on one company meant it contributed zero URLs, and the
    close pass read that silence as proof every one of its roles had been
    filled. Those rows were then closed permanently: the next successful
    run found their URLs already in Notion, took the update branch rather
    than creating them fresh, and left New This Run off — so they never
    came back into the active views.

    A scrape returning ZERO jobs counts as success. The company was
    reachable and genuinely has nothing open, so its old rows SHOULD
    close. Only an exception disqualifies.

    Companies with no matching scraper never enter the set either. They
    were never attempted, so their postings' absence proves exactly as
    little as a failure does.

    statuses is one record per company saying how its scrape went, in the
    shape sync_scrape_status() writes to Notion. Every record whose status
    is not SCRAPE_OK is a company no technique reached, and its note says
    which technique was tried and how it failed. That set used to be
    returned separately as `skipped`, but it is derivable from statuses by
    definition, and this repo has already been bitten once by keeping two
    independent records of the same fact and watching them drift
    (invariant 9). One record, one answer.
    """
    all_jobs = []
    scraped_ok = set()
    statuses = []

    def record(row, status, note=""):
        statuses.append({"row": row, "status": status, "note": note})

    for row in companies:
        scraper, matched_name = resolve_scraper(row["ats_platform"])

        # Workday takes its own path: its scraper needs four parameters
        # rather than one slug, and — unlike the slug platforms — it has no
        # guess-from-the-company-name fallback, because a guessed Workday
        # tenant can resolve to a real board belonging to someone else.
        if scraper and matched_name == "Workday":
            params = extract_workday_params(row.get("careers_url"))
            if not params:
                if not row.get("careers_url"):
                    print(f"[scrape] {row['company']}: no Careers URL — Workday board "
                          f"parameters can't be guessed, needs manual lookup")
                    record(row, SCRAPE_NO_URL,
                           "Workday: no Careers URL saved. A Workday board needs a real "
                           "host/tenant/site, which can't be guessed from the company "
                           "name, so this needs the board URL filled in by hand.")
                else:
                    print(f"[scrape] {row['company']}: saved Careers URL is not a Workday "
                          f"board — needs manual lookup")
                    record(row, SCRAPE_FAILED,
                           f"Workday: the saved Careers URL ({row['careers_url']}) is not a "
                           f"myworkdayjobs.com board URL, so the board parameters can't be "
                           f"read from it. Find the real board (usually linked from that "
                           f"page) and save it instead.")
                continue
            try:
                jobs = workday.fetch_jobs(row["company"], **params)
                for job in jobs:
                    job["company_description"] = row.get("sector")
                all_jobs.extend(jobs)
                scraped_ok.add(row["company"])
                filtered = " filtered to this company" if params["applied_facets"] else ""
                print(f"[scrape] {row['company']} (via Workday, "
                      f"{params['tenant']}/{params['site']}{filtered}): {len(jobs)} jobs")
                record(row, SCRAPE_OK, f"Workday: {len(jobs)} job(s)")
            except Exception as error:
                print(f"[scrape] {row['company']}: FAILED ({error})")
                record(row, SCRAPE_FAILED, f"Workday: {error}")
            continue

        if scraper:
            slug = extract_slug(row.get("careers_url"), matched_name)
            try:
                jobs = scraper(row["company"], slug=slug) if slug else scraper(row["company"])
                for job in jobs:
                    job["company_description"] = row.get("sector")
                all_jobs.extend(jobs)
                scraped_ok.add(row["company"])
                slug_note = f", slug from saved URL" if slug else ""
                print(f"[scrape] {row['company']} (via {matched_name}{slug_note}): {len(jobs)} jobs")
                record(row, SCRAPE_OK, f"{matched_name}: {len(jobs)} job(s)")
                continue
            except Exception as first_error:
                # If we had a confirmed slug and it still failed (URL moved,
                # company renamed on the platform, etc.), fall back to a
                # guessed slug as a second attempt before giving up.
                if slug:
                    try:
                        jobs = scraper(row["company"])  # no slug = falls back to guessing
                        for job in jobs:
                            job["company_description"] = row.get("sector")
                        all_jobs.extend(jobs)
                        scraped_ok.add(row["company"])
                        print(f"[scrape] {row['company']} (via {matched_name}, "
                              f"saved URL failed, guessed slug worked): {len(jobs)} jobs")
                        # Scraped fine, so not a manual-check case — but the
                        # saved Careers URL is stale, and saying so in the note
                        # is the only place that ever gets recorded.
                        record(row, SCRAPE_OK,
                               f"{matched_name}: {len(jobs)} job(s). The slug from the "
                               f"saved Careers URL failed and a guessed slug worked, so "
                               f"the saved URL is probably stale.")
                        continue
                    except Exception as second_error:
                        print(f"[scrape] {row['company']}: FAILED — both saved URL slug "
                              f"({first_error}) and guessed slug ({second_error}) failed")
                        record(row, SCRAPE_FAILED,
                               f"{matched_name}: the slug from the saved Careers URL "
                               f"failed ({first_error}) and a guessed slug failed too "
                               f"({second_error}). Check the company's real board URL.")
                        continue
                print(f"[scrape] {row['company']}: FAILED ({first_error}) "
                      f"— no saved Careers URL to fall back on, was guessing the slug")
                record(row, SCRAPE_FAILED,
                       f"{matched_name}: {first_error}. There is no saved Careers URL to "
                       f"take a real slug from, so the slug was guessed from the company "
                       f"name. Saving the real board URL would likely fix this.")
                continue

        # No API-backed scraper matched — try the generic HTML scraper if
        # this company is marked as HTML-scrapeable and has a saved URL
        # (either a real careers page or a homepage fallback — both are
        # genuine, scrapeable company pages).
        is_html = row["scrape_method"] and "HTML" in row["scrape_method"]
        if is_html and row.get("careers_url"):
            try:
                jobs = html_generic.fetch_jobs(row["company"], row["careers_url"])
                for job in jobs:
                    job["company_description"] = row.get("sector")
                all_jobs.extend(jobs)
                scraped_ok.add(row["company"])
                print(f"[scrape] {row['company']} (via HTML): {len(jobs)} jobs")
                record(row, SCRAPE_OK, f"HTML: {len(jobs)} job(s)")
            except Exception as e:
                print(f"[scrape] {row['company']} (HTML): FAILED ({e})")
                record(row, SCRAPE_FAILED,
                       f"HTML scrape of {row['careers_url']} failed: {e}")
        elif is_html:
            # Marked HTML-scrapeable, but step 2b never managed to save a URL
            # to point the scraper at — not even the Google-search fallback.
            record(row, SCRAPE_NO_URL,
                   "Marked HTML-scrapeable but has no saved Careers URL to fetch. "
                   "The Careers URL backfill has already tried and come up empty.")
        else:
            ats = row["ats_platform"] or "(blank)"
            method = row["scrape_method"] or "(blank)"
            record(row, SCRAPE_NO_SCRAPER,
                   f"No scraper handles ATS Platform \"{ats}\" with Scrape Method "
                   f"\"{method}\". Either the platform needs a scraper written, or the "
                   f"row needs marking as HTML-scrapeable.")

    return all_jobs, scraped_ok, statuses


def main():
    parser = argparse.ArgumentParser(description="Job Search Agent pipeline")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Skip every Notion write and Anthropic API call; print what would happen instead.",
    )
    args = parser.parse_args()
    dry_run = args.dry_run

    if dry_run:
        print("=== DRY RUN — no Notion writes or Anthropic calls will be made ===")

    print("=== Step 1-2: checking Notion for companies missing ATS data ===")
    with timed("1  fill_missing_ats"):
        fill_missing_ats(dry_run=dry_run)

    print("\n=== Step 2a: backfilling sector/HQ for companies missing it ===")
    with timed("2  fill_missing_profile_fields"):
        fill_missing_profile_fields(dry_run=dry_run)

    print("\n=== Step 2b: checking for HTML-scrape companies missing a Careers URL ===")
    with timed("3  fill_missing_careers_urls"):
        fill_missing_careers_urls(dry_run=dry_run)

    print("\n=== Step 2c: filling in company Website + logo where derivable ===")
    with timed("3b fill_missing_website"):
        fill_missing_website(dry_run=dry_run)

    print("\n=== Step 3: re-pulling full company list ===")
    with timed("3a get_all_companies"):
        companies = get_all_companies()
    print(f"{len(companies)} total companies")

    print("\n=== Step 4-5: scraping API-backed companies ===")
    with timed("4  run_scrapers"):
        jobs, scraped_ok, statuses = run_scrapers(companies)
    print(f"{len(scraped_ok)} company(ies) scraped successfully; only their postings "
          f"are eligible to be marked closed below")

    print("\n=== Step 5a: stamping scrape status onto the Target List ===")
    needs_check = [s for s in statuses if s["status"] != SCRAPE_OK]
    with timed("5  sync_scrape_status"):
        status_result = sync_scrape_status(statuses, dry_run=dry_run)
    tail = f", {status_result['failed']} failed to write" if status_result["failed"] else ""
    print(f"{status_result['written']} row(s) restamped, "
          f"{status_result['unchanged']} already correct{tail}")
    print(f"{len(needs_check)} company(ies) need a manual look — they are the "
          f"\"Needs Manual Check\" view on the Target List in Notion:")
    for entry in needs_check[:15]:
        print(f"       - {entry['row']['company']} [{entry['status']}]: {entry['note']}")
    if len(needs_check) > 15:
        print(f"       ... and {len(needs_check) - 15} more")

    print("\n=== Step 6: flagging new postings vs. previous runs ===")
    with timed("6  mark_new_postings"):
        jobs = mark_new_postings(jobs, dry_run=dry_run)
    new_count = sum(1 for j in jobs if j["is_new"])
    print(f"{new_count} new posting(s) since the last run, {len(jobs) - new_count} already seen before")

    print("\n=== Step 7: scoring jobs against the rubric ===")
    with timed("7  score_all"):
        jobs = score_all(jobs, dry_run=dry_run)
    print(f"Scored {len(jobs)} jobs")

    print("\n=== Step 8: syncing jobs to Notion ===")
    with timed("8  sync_jobs_to_notion"):
        sync_result = sync_jobs_to_notion(jobs, scraped_ok, dry_run=dry_run,
                                          rubric_fingerprint=RUBRIC_FINGERPRINT)
    print(f"{'[dry-run] would sync' if dry_run else 'Notion'}: "
          f"{sync_result['created']} created, {sync_result['updated']} updated, "
          f"{sync_result['unchanged']} unchanged (not written), "
          f"{sync_result['closed']} marked closed (no longer posted)")
    if sync_result.get("date_applied_backfilled"):
        print(f"{'[dry-run] would stamp' if dry_run else 'Stamped'} Date Applied on "
              f"{sync_result['date_applied_backfilled']} posting(s) marked Applied "
              f"in Notion with no date yet")
    if sync_result.get("close_aborted"):
        print("[warn] the close pass was aborted by the 25% safety valve — see above. "
              "Nothing was closed; investigate the scrape failures before the next run.")
    if sync_result.get("failed"):
        print(f"[warn] {sync_result['failed']} job(s) failed to sync after retries "
              f"(likely a transient Notion outage) — they'll be retried automatically next run:")
        for fj in sync_result["failed_jobs"][:10]:
            print(f"       - {fj['company']}: {fj['title']}")
        if sync_result["failed"] > 10:
            print(f"       ... and {sync_result['failed'] - 10} more")

    with open("output/jobs.json", "w") as f:
        json.dump(jobs, f, indent=2)

    # Same set as the Notion view, kept on disk for the Excel tab and for
    # diffing one run against another without going through the API.
    with open("output/skipped_companies.json", "w") as f:
        json.dump([{
            "company": e["row"]["company"],
            "sector": e["row"].get("sector"),
            "ats_platform": e["row"]["ats_platform"],
            "careers_url": e["row"].get("careers_url"),
            "status": e["status"],
            "note": e["note"],
        } for e in needs_check], f, indent=2)

    with timed("9  build_workbook"):
        excel_path = build_workbook()

    print(f"\nDone. {len(jobs)} jobs written to output/jobs.json")
    print(f"{len(needs_check)} companies need manual checking — see the "
          f"\"Needs Manual Check\" view in Notion, or output/skipped_companies.json")
    print(f"Formatted Excel version: {excel_path}")

    print_timing_report()
    print_api_report(dry_run=dry_run)


if __name__ == "__main__":
    main()

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
"""
import json
import os
import re
import time
from urllib.parse import quote_plus

from dotenv import load_dotenv
load_dotenv()  # reads the .env file in this folder and loads NOTION_API_KEY, etc.

from notion.client import (
    get_all_companies, get_companies_missing_ats, get_companies_missing_profile,
    get_companies_missing_url, update_ats_fields, update_company_info,
    sync_jobs_to_notion,
)
from ats_finder.find_ats import find_ats, find_company_info, find_via_search
from scrapers import greenhouse, lever, ashby, html_generic
from scoring.score_jobs import score_all
from scoring.track_new_postings import mark_new_postings
from output_excel import build_workbook

SCRAPERS = {
    "Greenhouse": greenhouse.fetch_jobs,
    "Lever": lever.fetch_jobs,
    "Ashby": ashby.fetch_jobs,
}


def fill_missing_profile_fields():
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
    """
    missing = get_companies_missing_profile()
    if not missing:
        print("[check] Every company already has sector/HQ filled in. Continuing.")
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


def fill_missing_ats():
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
    """
    missing = get_companies_missing_ats()
    if not missing:
        print("[check] Every company already has an ATS value. Continuing.")
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


def fill_missing_careers_urls():
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
    """
    missing = get_companies_missing_url()
    if not missing:
        print("[check] Every company already has a Careers URL. Continuing.")
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
    "Ashby": [r"jobs\.ashbyhq\.com/([a-zA-Z0-9\-_]+)"],
}


def extract_slug(careers_url, platform_name):
    """Pull the real ATS slug out of a saved Careers URL, if possible."""
    if not careers_url or platform_name not in SLUG_URL_PATTERNS:
        return None
    for pattern in SLUG_URL_PATTERNS[platform_name]:
        m = re.search(pattern, careers_url)
        if m:
            return m.group(1)
    return None


def run_scrapers(companies):
    all_jobs = []
    skipped = []

    for row in companies:
        scraper, matched_name = resolve_scraper(row["ats_platform"])

        if scraper:
            slug = extract_slug(row.get("careers_url"), matched_name)
            try:
                jobs = scraper(row["company"], slug=slug) if slug else scraper(row["company"])
                for job in jobs:
                    job["company_description"] = row.get("sector")
                all_jobs.extend(jobs)
                slug_note = f", slug from saved URL" if slug else ""
                print(f"[scrape] {row['company']} (via {matched_name}{slug_note}): {len(jobs)} jobs")
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
                        print(f"[scrape] {row['company']} (via {matched_name}, "
                              f"saved URL failed, guessed slug worked): {len(jobs)} jobs")
                        continue
                    except Exception as second_error:
                        print(f"[scrape] {row['company']}: FAILED — both saved URL slug "
                              f"({first_error}) and guessed slug ({second_error}) failed")
                        skipped.append(row)
                        continue
                print(f"[scrape] {row['company']}: FAILED ({first_error}) "
                      f"— no saved Careers URL to fall back on, was guessing the slug")
                skipped.append(row)
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
                print(f"[scrape] {row['company']} (via HTML): {len(jobs)} jobs")
            except Exception as e:
                print(f"[scrape] {row['company']} (HTML): FAILED ({e})")
                skipped.append(row)
        else:
            skipped.append(row)

    return all_jobs, skipped


def main():
    print("=== Step 1-2: checking Notion for companies missing ATS data ===")
    fill_missing_ats()

    print("\n=== Step 2a: backfilling sector/HQ for companies missing it ===")
    fill_missing_profile_fields()

    print("\n=== Step 2b: checking for HTML-scrape companies missing a Careers URL ===")
    fill_missing_careers_urls()

    print("\n=== Step 3: re-pulling full company list ===")
    companies = get_all_companies()
    print(f"{len(companies)} total companies")

    print("\n=== Step 4-5: scraping API-backed companies ===")
    jobs, skipped = run_scrapers(companies)

    print("\n=== Step 6: flagging new postings vs. previous runs ===")
    jobs = mark_new_postings(jobs)
    new_count = sum(1 for j in jobs if j["is_new"])
    print(f"{new_count} new posting(s) since the last run, {len(jobs) - new_count} already seen before")

    print("\n=== Step 7: scoring jobs against the rubric ===")
    jobs = score_all(jobs)
    print(f"Scored {len(jobs)} jobs")

    print("\n=== Step 8: syncing jobs to Notion ===")
    sync_result = sync_jobs_to_notion(jobs)
    print(f"Notion: {sync_result['created']} created, {sync_result['updated']} updated, "
          f"{sync_result['closed']} marked closed (no longer posted)")
    if sync_result.get("failed"):
        print(f"[warn] {sync_result['failed']} job(s) failed to sync after retries "
              f"(likely a transient Notion outage) — they'll be retried automatically next run:")
        for fj in sync_result["failed_jobs"][:10]:
            print(f"       - {fj['company']}: {fj['title']}")
        if sync_result["failed"] > 10:
            print(f"       ... and {sync_result['failed'] - 10} more")

    with open("output/jobs.json", "w") as f:
        json.dump(jobs, f, indent=2)

    with open("output/skipped_companies.json", "w") as f:
        json.dump([{"company": s["company"], "sector": s.get("sector"), "ats_platform": s["ats_platform"]} for s in skipped], f, indent=2)

    excel_path = build_workbook()

    print(f"\nDone. {len(jobs)} jobs written to output/jobs.json")
    print(f"{len(skipped)} companies need manual/HTML scraping — see output/skipped_companies.json")
    print(f"Formatted Excel version: {excel_path}")


if __name__ == "__main__":
    main()

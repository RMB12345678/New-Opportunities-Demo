"""
Thin wrapper around the Notion API for the Target List database.

Requires env var NOTION_API_KEY (a Notion internal integration token,
created at https://www.notion.so/my-integrations, then shared with the
Target List database via "Connections" in Notion's UI).

Requires env var NOTION_DATABASE_ID (the ID of the Target List database,
found in its page URL).
"""
import collections
import os
import time
import requests

from http_client import session
from notion import state

NOTION_API_KEY = os.environ["NOTION_API_KEY"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
JOB_POSTINGS_DATABASE_ID = os.environ["JOB_POSTINGS_DATABASE_ID"]
NOTION_VERSION = "2022-06-28"

# Close-pass safety valve. A single run closing more than this fraction of
# everything currently open is a systemic failure, not real closures.
CLOSE_VALVE_MAX_FRACTION = 0.25

# ...but a fraction is meaningless on a handful of rows: with 3 open rows,
# one genuine closure is 33% and would abort, and would keep aborting on
# every subsequent run, so that row could never close at all. Below this
# floor the ratio is not applied and closures go through on their own
# merits. 20 is the point where a single closure (5%) is comfortably
# inside the limit, so the valve only speaks up about genuine clusters.
CLOSE_VALVE_MIN_OPEN_ROWS = 20

# Scrape Status values stamped onto every Target List row after each run.
# Anything other than SCRAPE_OK means no technique reached that company's
# postings, and those rows are what the "Needs Manual Check" view in Notion
# filters for. They are deliberately coarse: the Scrape Note carries the
# detail, the status carries the shape of the problem, and the shape is what
# decides what fixing it involves (a broken slug is not a missing URL is not
# an ATS nobody has written a scraper for).
SCRAPE_OK = "OK"                      # a scraper ran and returned; zero jobs counts
SCRAPE_FAILED = "Failed"              # a scraper ran and raised
SCRAPE_NO_SCRAPER = "No Scraper"      # nothing matched this ATS Platform value
SCRAPE_NO_URL = "No Careers URL"      # marked HTML-scrapeable with nothing to fetch

BASE_URL = "https://api.notion.com/v1"
HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}


# --- API call accounting ---------------------------------------------
# Every Notion request this module makes is funnelled through one of the
# helpers that bump these counters, so they are the only count that cannot
# drift from what actually went over the wire. Writes are counted even
# under --dry-run, where the helper returns before the request: the whole
# point of a dry run is to see the write volume a real run *would* cost,
# and a counter that only counts real writes would report zero for it.
API_CALLS = collections.Counter()


def _count(kind):
    API_CALLS[kind] += 1


def reset_api_calls():
    API_CALLS.clear()


def api_call_report():
    """Reads and writes split out, since they cost very differently: a read
    pages 100 rows at a time, a write is one row and carries a throttle
    sleep. A run dominated by writes is a run with a delta problem."""
    reads = {k: v for k, v in API_CALLS.items() if k.startswith("read:")}
    writes = {k: v for k, v in API_CALLS.items() if k.startswith("write:")}
    return {
        "reads": reads, "writes": writes,
        "read_total": sum(reads.values()), "write_total": sum(writes.values()),
    }


def _raise_with_detail(resp):
    """Raise with Notion's actual error message instead of the generic
    '400 Client Error: Bad Request' that requests.raise_for_status() gives —
    that generic message hides the real reason (invalid property value,
    unknown field, rate limit, etc.), which is exactly what made previous
    failures impossible to diagnose from the console output alone."""
    if resp.ok:
        return
    try:
        body = resp.json()
        detail = body.get("message") or body.get("code") or resp.text
    except Exception:
        detail = resp.text
    raise requests.exceptions.HTTPError(
        f"{resp.status_code} error from Notion API: {detail}", response=resp
    )


def get_all_companies():
    """Return every row in the Target List database as a list of dicts:
    [{page_id, company, sector, hq, source, ats_platform, scrape_method,
      careers_url, scrape_status, scrape_note, failing_since, website,
      has_icon}, ...]
    """
    rows = []
    payload = {"page_size": 100}
    url = f"{BASE_URL}/databases/{NOTION_DATABASE_ID}/query"

    while True:
        _count("read:target_list_page")
        resp = session.post(url, headers=HEADERS, json=payload)
        _raise_with_detail(resp)
        data = resp.json()

        for page in data["results"]:
            props = page["properties"]
            rows.append({
                "page_id": page["id"],
                "company": _plain_text(props.get("Company")),
                "sector": _plain_text(props.get("Sector / Focus")),
                "hq": _plain_text(props.get("HQ")),
                "source": _plain_text(props.get("Source")),
                "ats_platform": _plain_text(props.get("ATS Platform")),
                "scrape_method": _plain_text(props.get("Scrape Method")),
                "careers_url": _url_value(props.get("Careers URL")),
                # Read back so sync_scrape_status() can skip rows whose stamp
                # hasn't moved since the last run — see its docstring.
                "scrape_status": _select_name(props.get("Scrape Status")),
                "scrape_note": _plain_text(props.get("Scrape Note")),
                "failing_since": _date_start(props.get("Failing Since")),
                "website": _url_value(props.get("Website")),
                # Page-level, not a property — see
                # get_companies_missing_website()'s docstring for why this
                # is read here rather than derived from "website" alone.
                "has_icon": page.get("icon") is not None,
            })

        if not data.get("has_more"):
            break
        payload["start_cursor"] = data["next_cursor"]

    return rows


def get_companies_missing_ats():
    """Return only rows where ATS Platform is empty."""
    return [row for row in get_all_companies() if not row["ats_platform"]]


def get_companies_missing_profile():
    """Return rows that have an ATS Platform filled in (so they won't be
    caught by get_companies_missing_ats) but are still missing Sector or
    HQ — e.g. companies added before the full-profile auto-fill existed,
    or ones that hit the old fast-path-only bug."""
    return [
        row for row in get_all_companies()
        if row["ats_platform"] and "AMBIGUOUS" not in row["ats_platform"]
        and (not row["sector"] or not row["hq"])
    ]


def get_companies_missing_url():
    """Return ANY company missing a Careers URL, regardless of scrape
    method — this used to only check HTML-scrape companies, which meant
    API-tier companies (Greenhouse/Lever/Ashby) that already had their ATS,
    sector, and HQ confirmed long ago NEVER got a real URL saved, since no
    other backfill pass ever revisits a company once those three fields are
    filled. That gap meant the API scrapers were stuck permanently guessing
    a slug from the company name instead of using the real, confirmed one —
    exactly why previously-verified companies like Adaptive Innovations
    kept 404ing on every run despite us knowing their real ATS URL.

    Excludes companies whose ATS Platform already says "Not found" —
    that's a conclusive "nothing exists here" answer for the company as a
    whole (not just its careers page specifically), so searching AGAIN for
    just a URL on those companies is pure waste: we already know the
    answer. Only "AMBIGUOUS"/"N/A"/"Not found" markers are excluded;
    companies with a real ATS platform (even "None (in-house...)") still
    get searched, since those genuinely might have a findable page."""
    return [
        row for row in get_all_companies()
        if row["ats_platform"]
        and "AMBIGUOUS" not in row["ats_platform"]
        and "N/A" not in row["ats_platform"]
        and "Not found" not in row["ats_platform"]
        and not row["careers_url"]
    ]


# Kept for backward compatibility with any code still importing the old name.
get_html_companies_missing_url = get_companies_missing_url


def get_companies_missing_website():
    """Return companies with no confirmed Website AND no page icon yet.

    Website is a Notion URL-type property, so — unlike ATS Platform — it
    can't hold a text marker such as "Not found" to remember a company that
    was genuinely searched and came up empty. Without some marker,
    invariant 8 (every "nothing found" outcome writes a non-blank value)
    can't be satisfied here, and the same company would be re-searched, and
    re-paid for, on every future run.

    The page ICON carries that memory instead. Every company
    ats_finder.find_website.derive_website() ever looks at gets a page icon
    stamped one way or the other — a real favicon on success, or the plain
    white square (BLANK_ICON) on a genuine "nothing found" — so "no icon
    yet" is the real "never processed" signal, not "no Website value"
    alone. Trade-off worth knowing: a company whose icon a person set by
    hand for an unrelated reason will look identical to an already-checked
    one and won't be retried.
    """
    return [row for row in get_all_companies() if not row["website"] and not row["has_icon"]]


def update_company_website(page_id, website=None, icon=None, dry_run=False):
    """Write a company's derived Website URL and/or page icon.

    Same None-means-don't-touch convention as update_company_info(), with
    one deliberate difference: an icon of BLANK_ICON ("⬜") is a real value
    to write — the "checked, nothing found" stamp — not a signal to skip.
    Only Python None skips a field, exactly as everywhere else in this
    module.
    """
    properties = {}
    if website is not None:
        properties["Website"] = {"url": website or None}

    payload = {}
    if properties:
        payload["properties"] = properties
    if icon is not None:
        if icon.startswith("http://") or icon.startswith("https://"):
            payload["icon"] = {"type": "external", "external": {"url": icon}}
        else:
            payload["icon"] = {"type": "emoji", "emoji": icon}

    if not payload:
        return None

    _count("write:company_website")
    if dry_run:
        print(f"     [dry-run] would update {page_id}: "
              f"website={website!r} icon={'set' if icon else None}")
        return None

    resp = session.patch(f"{BASE_URL}/pages/{page_id}", headers=HEADERS, json=payload)
    _raise_with_detail(resp)
    return resp.json()


def update_company_info(page_id, sector=None, hq=None, source=None,
                          ats_platform=None, scrape_method=None, careers_url=None,
                          dry_run=False):
    """Write any subset of a company's fields back to its Notion page.
    Only fields passed in (not None) are written — this lets callers
    update just ATS info, just sector/HQ, or everything at once."""
    url = f"{BASE_URL}/pages/{page_id}"
    properties = {}

    text_field_map = {
        "Sector / Focus": sector,
        "HQ": hq,
        "Source": source,
        "ATS Platform": ats_platform,
        "Scrape Method": scrape_method,
    }
    for notion_field, value in text_field_map.items():
        if value is not None:
            if value == "":
                properties[notion_field] = {"rich_text": []}  # actually clears the field
            else:
                properties[notion_field] = {"rich_text": [{"text": {"content": value[:2000]}}]}

    # Careers URL is a real Notion URL-type property (so it renders as a
    # clickable link), not text — it needs the {"url": ...} shape, and an
    # empty string must become None rather than "", which Notion also rejects.
    if careers_url is not None:
        properties["Careers URL"] = {"url": careers_url or None}

    if not properties:
        return None

    _count("write:company_info")
    if dry_run:
        print(f"     [dry-run] would update {page_id}: {list(properties.keys())}")
        return None

    payload = {"properties": properties}
    resp = session.patch(url, headers=HEADERS, json=payload)
    _raise_with_detail(resp)
    return resp.json()


def update_ats_fields(page_id, ats_platform, scrape_method, careers_url=None, dry_run=False):
    """Kept for backward compatibility — writes just the ATS-related fields."""
    return update_company_info(
        page_id, ats_platform=ats_platform, scrape_method=scrape_method,
        careers_url=careers_url, dry_run=dry_run
    )


def sync_scrape_status(statuses, dry_run=False):
    """Stamp every Target List row with how this run's scrape of it went.

    `statuses` is what run_scrapers() collected: one record per company,
    holding the row it came from, the outcome, and a note explaining it.
    Every record whose status is not SCRAPE_OK is a company no scraping
    technique could cover, which is exactly the set the "Needs Manual
    Check" view in Notion shows. That view is the point of this function.
    Before it existed, a company that failed every single run just sat in
    output/skipped_companies.json — a file nobody opens — and stayed
    broken indefinitely, because nothing ever surfaced it.

    Two deliberate departures from how the rest of this module writes:

    - It overwrites non-empty fields, which invariant 3 forbids for the
      backfill paths. That is correct here and only here: these three
      properties are a status stamp owned by the pipeline, not researched
      facts a human might have corrected by hand. Nothing else should copy
      this pattern.

    - It writes only rows whose stamp actually changed. A healthy run has
      several hundred companies scraping fine with nothing new to say, and
      PATCHing every one of them to rewrite identical values would add
      minutes of wall clock against Notion's rate limit for no information
      gained.

    `Failing Since` answers the question the view exists to answer — how
    long has this been broken — so it is set on the transition INTO a
    failing state and left alone while the company keeps failing. Coming
    back to OK clears it. Re-stamping it every run would make it a slower
    way of saying "today", which tells you nothing.
    """
    from datetime import date
    today = date.today().isoformat()

    written, unchanged, failed = 0, 0, 0

    for record in statuses:
        row = record["row"]
        status = record["status"]
        note = record.get("note") or ""

        was_failing = row.get("scrape_status") not in (None, SCRAPE_OK)
        if status == SCRAPE_OK:
            failing_since = None
        elif was_failing and row.get("failing_since"):
            failing_since = row["failing_since"]  # already broken; keep the original date
        else:
            failing_since = today

        if (status == row.get("scrape_status")
                and note == (row.get("scrape_note") or "")
                and failing_since == row.get("failing_since")):
            unchanged += 1
            continue

        try:
            _write_scrape_status(row["page_id"], status, note, failing_since, dry_run=dry_run)
            written += 1
        except Exception as e:
            # Same reasoning as the job sync below: one row's PATCH failing
            # is not worth losing everything that comes after this call. The
            # stamp is derived state and the next run recomputes it from
            # scratch, so a miss here costs one run's visibility, nothing more.
            print(f"     [status write failed] {row.get('company')}: {e}")
            failed += 1

    return {"written": written, "unchanged": unchanged, "failed": failed}


def _write_scrape_status(page_id, status, note, failing_since, dry_run=False):
    """Write the three Scrape Status properties onto one Target List page.

    Scrape Status is a Notion select, so the value has to be one of the
    options defined on the property — the SCRAPE_* constants above are those
    names and have to stay in step with them. Failing Since is a date
    property, where clearing it means {"date": None}; an empty dict instead
    is a malformed date and Notion rejects the whole request.
    """
    properties = {
        "Scrape Status": {"select": {"name": status}},
        "Scrape Note": (
            {"rich_text": [{"text": {"content": note[:2000]}}]} if note
            else {"rich_text": []}
        ),
        "Failing Since": {"date": {"start": failing_since} if failing_since else None},
    }

    _count("write:scrape_status")
    if dry_run:
        print(f"     [dry-run] would set Scrape Status = {status} on {page_id}")
        return None

    resp = session.patch(f"{BASE_URL}/pages/{page_id}", headers=HEADERS,
                         json={"properties": properties})
    _raise_with_detail(resp)
    return resp.json()


# Notion truncates nothing on its own — _build_job_properties() does, at
# 2000 characters. Any comparison has to truncate the desired value the
# same way, or a longer-than-2000 Reasoning would read back shorter than
# what we meant to write, compare unequal, and rewrite its row on every
# run forever. Same class of bug as the one _date_start() documents.
TEXT_LIMIT = 2000


def _norm_text(value):
    """Normalise a text field so the two sides of a diff are comparable.

    Notion returns None for an empty rich_text; the scrapers return "".
    Left alone, that difference alone would mark every job with no
    Ambiguity Note as changed on every run.
    """
    return (value or "")[:TEXT_LIMIT]


def _scores_equal(desired, current):
    """Score is a Notion number, so it can come back as 7.0 where the
    scorer produced 7. Comparing those with == would rewrite the row every
    run for no reason."""
    if desired is None or current is None:
        return desired is None and current is None
    return abs(float(desired) - float(current)) < 1e-9


def _selects_equal(desired, current):
    """Compare a Notion select case-insensitively.

    Notion matches an incoming select name against its existing options
    case-insensitively and then returns its OWN canonical casing. The
    pipeline emits "Needs review"; the option in the database is named
    "Needs Review", so every write was accepted, stored as "Needs Review",
    and read back differing from what we asked for.

    Under a naive == that is a change that never resolves: it rewrote 745
    rows on every run, forever, while the sync reported itself healthy.
    That was 93% of the writes left after the delta pass and is exactly the
    trap invariant 13 is about — caught only because the dry run prints the
    reason for each planned write, and the reason read
    "Routing: Needs Review -> Needs review".

    Fixing this here rather than in the scorer is deliberate: the scorer's
    literal is also what output_excel.py filters its Needs Review tab on,
    so changing the emitted string to match Notion's casing would silently
    empty that tab.
    """
    if desired is None or current is None:
        return desired is None and current is None
    return desired.strip().casefold() == current.strip().casefold()


def _desired_job_fields(job):
    """The value this pipeline wants each owned property to hold, normalised
    for comparison. Deliberately excludes Last Seen, First Seen, Still
    Open, New This Run and the Company relation: those are not derived
    from the job payload the way these are, and each has its own rule
    about when it may be written. Also excludes Date Applied and
    Application Notes, which belong to the human, not the pipeline."""
    return {
        "title": _norm_text(job.get("title") or "(untitled)"),
        "Score": job.get("score"),
        "Routing": job.get("routing"),
        "Reasoning": _norm_text(job.get("reasoning")),
        "Ambiguity Note": _norm_text(job.get("ambiguity_note")),
        "IC Role": bool(job.get("ic_role_flag")),
        "Location": _norm_text(job.get("location")),
        "ATS": _norm_text(job.get("source_ats")),
    }


def _current_job_fields(row):
    """The same set of properties as _desired_job_fields(), read back off
    the Notion row and normalised identically so the two can be compared
    key by key."""
    return {
        "title": _norm_text(row.get("title")),
        "Score": row.get("score"),
        "Routing": row.get("routing"),
        "Reasoning": _norm_text(row.get("reasoning")),
        "Ambiguity Note": _norm_text(row.get("ambiguity_note")),
        "IC Role": bool(row.get("ic_role")),
        "Location": _norm_text(row.get("location")),
        "ATS": _norm_text(row.get("ats")),
    }


def _to_write_value(key, value):
    """Translate a normalised comparison value back into the flat form
    _build_job_properties() expects."""
    if key == "IC Role":
        return "__YES__" if value else "__NO__"
    return value


def _desired_write_properties(job, company_page_id):
    """Every owned property, in write form. Used when creating a row,
    where there is nothing to diff against and all of it has to go."""
    properties = {key: _to_write_value(key, value)
                  for key, value in _desired_job_fields(job).items()}
    if company_page_id:
        properties["Company"] = [company_page_id]
    return properties


def _changed_job_properties(job, row, company_page_id):
    """Return only the properties whose value actually differs from what
    Notion holds for this row, in write form. An empty dict means the row
    is already correct and must not be written at all — that is where the
    entire saving comes from.
    """
    desired = _desired_job_fields(job)
    current = _current_job_fields(row)

    changed = {}
    for key, want in desired.items():
        # None means "no answer this run", not "clear the field" — the same
        # convention update_company_info() uses, and for the same reason. A
        # job whose scoring failed comes back with score and routing None;
        # writing that would blank a good score, and _build_job_properties()
        # drops None anyway, so the PATCH would go out carrying nothing.
        if want is None:
            continue
        have = current.get(key)
        if key == "Score":
            same = _scores_equal(want, have)
        elif key == "Routing":
            same = _selects_equal(want, have)
        else:
            same = want == have
        if not same:
            changed[key] = _to_write_value(key, want)

    # Still Open: this URL came back in today's scrape, so the posting is
    # live. Only written when the row currently disagrees — which happens
    # when a posting closed and later reappeared, and is the one path that
    # brings a closed row back into the Scored List view.
    if not row.get("still_open"):
        changed["Still Open"] = "__YES__"

    # New This Run: false for anything that already existed in Notion.
    # Written only where the box is actually ticked, which in a steady
    # state is just the previous run's creations — tens of rows rather
    # than the whole table. This is the same answer the old code wrote on
    # every row every run; the only change is asking first.
    if row.get("new_this_run"):
        changed["New This Run"] = "__NO__"

    # A missing company_page_id means the company isn't in the Target List
    # under this name. Leave the existing relation alone rather than
    # clearing it: an unresolved relation is what stops the close pass
    # touching a row, so silently blanking one would put a live posting
    # permanently beyond the reach of both passes.
    if company_page_id and row.get("company_page_id") != company_page_id:
        changed["Company"] = [company_page_id]

    return changed


def _describe_changes(job, row, changed):
    """Human-readable reason for a planned write, for the dry-run log.
    Names the field and what it is moving from and to, because "would
    update page X" on its own gives a reader no way to tell a real change
    from a comparison bug."""
    current = _current_job_fields(row)
    desired = _desired_job_fields(job)
    parts = []
    for key in sorted(changed):
        if key == "Still Open":
            parts.append("reopened (Still Open false -> true)")
        elif key == "New This Run":
            parts.append("clearing last run's New This Run flag")
        elif key == "Company":
            parts.append("Company relation repointed")
        else:
            before = current.get(key)
            after = desired.get(key)
            parts.append(f"{key}: {_short(before)} -> {_short(after)}")
    return "; ".join(parts)


def _short(value):
    text = "(empty)" if value in (None, "") else str(value)
    return text if len(text) <= 40 else text[:37] + "..."


def _log_planned_write(page_id, properties, reason):
    print(f"     [dry-run] WRITE {page_id}  fields={sorted(properties)}  reason={reason}")


def sync_jobs_to_notion(jobs, scraped_ok, dry_run=False, rubric_fingerprint=None):
    """Write scored jobs to the Job Postings Notion database, deduped by URL.

    - New URL -> create a row, First Seen = today, linked to the matching
      Target List company via the Company relation.
    - Known URL -> compare every property this pipeline owns against what
      Notion already holds, and PATCH only the ones that actually moved.
      A row with nothing new to say costs zero writes.
    - A row whose URL did NOT show up in this run's scrape gets Still Open
      flipped to false (not deleted — the history stays, it's just marked
      closed), but ONLY when its company is in scraped_ok. See the close
      pass below for why that qualifier is the entire point.

    scraped_ok comes from run_scrapers() and holds the names of the
    companies whose scrape actually completed this run.

    Why the diff exists
    -------------------
    This function used to PATCH all twelve owned properties onto every job
    on every run. At 3,200 postings that is 3,200 writes, each carrying a
    0.35s throttle sleep, so the sync alone took about half an hour and
    grew linearly with the table — while writing byte-identical values to
    roughly 3,150 of those rows. sync_scrape_status() had already learned
    this lesson on the Target List; the reasoning just never got carried
    across to Job Postings.

    The diff compares against Notion's own state, not a local mirror.
    Every field it needs is read back by _query_all_job_postings() out of
    the paginated query this function was already running, so the
    comparison costs no extra request. A local mirror of row state was the
    obvious alternative and is the wrong answer: it is a second
    independent record of facts Notion already holds, which is exactly the
    drift invariant 9 exists to prevent. output/notion_state.json
    therefore keeps only what Notion cannot answer — see notion/state.py.

    rubric_fingerprint is used for reporting only. A changed rubric means
    scores genuinely moved and the value comparison will notice on its
    own; the fingerprint just lets the run say so up front, instead of
    leaving a reader to wonder why a normally-quiet sync suddenly wrote
    three thousand rows. Gating score writes on the fingerprint *instead*
    of on the value would be strictly worse: it would skip a row whose
    previous write failed, and would never heal a score edited by hand.
    """
    from datetime import date
    today = date.today().isoformat()

    saved_state = state.load()
    previous_fingerprint = saved_state.get("rubric_fingerprint")
    if rubric_fingerprint and previous_fingerprint and previous_fingerprint != rubric_fingerprint:
        print(f"     [rubric] fingerprint changed ({previous_fingerprint} -> "
              f"{rubric_fingerprint}): scores have been recomputed, so expect this "
              f"sync to write most rows. That is correct, not a regression.")

    # The date to stamp on a posting that closes this run. The run that
    # closes a job is by definition the run that did NOT see it, so
    # stamping today would record the opposite of what Last Seen means;
    # the previous run is the last one that did see it. Falls back to
    # today when no previous run is recorded (the first run after this
    # shipped), which is off by at most one run's interval and corrects
    # itself immediately afterwards.
    last_seen_on_close = saved_state.get("last_run") or today

    # Target List has to be fetched first now: _query_all_job_postings()
    # needs this map to resolve each row's Company relation to a name,
    # which is what the close pass gates on.
    company_pages = _query_all_target_list_pages()
    company_page_by_name = {row["company"]: row["page_id"] for row in company_pages}
    company_name_by_page_id = {row["page_id"]: row["company"] for row in company_pages}
    # Lets both branches below copy a company's own page icon onto its job
    # postings — Job Postings has no icon-setting logic of its own, so
    # without this every row stays blank forever. Only companies with a
    # real external icon (a resolved favicon) show up here.
    company_icon_by_page_id = {row["page_id"]: row["icon_url"]
                                for row in company_pages if row.get("icon_url")}

    existing = _query_all_job_postings(company_name_by_page_id)
    existing_by_url = {row["url"]: row for row in existing if row.get("url")}

    # Application Status is set by hand in Notion; a real "stamp the date
    # the moment the status changes" automation needs a paid Notion plan
    # (see punch list), so this is the free substitute. Runs against every
    # row Notion currently holds, not just this run's scrape, so it still
    # catches a posting marked Applied after it has already closed and
    # stopped showing up here, which the create/update loop below would
    # otherwise never revisit.
    date_applied_backfilled = _backfill_applied_dates(existing, today, dry_run=dry_run)

    seen_urls_this_run = set()
    created, updated, unchanged, failed = 0, 0, 0, 0
    failed_jobs = []

    for job in jobs:
        url = job.get("url")
        if not url:
            continue  # can't dedup or relate without a URL, skip rather than risk a duplicate
        seen_urls_this_run.add(url)

        company_page_id = company_page_by_name.get(job.get("company"))
        row = existing_by_url.get(url)
        # Notion's own, authoritative answer to "is this new" — this is what
        # "New This Run" is based on, instead of the separate local
        # seen_jobs.json file. Using two independent sources for the same
        # question let them drift out of sync (e.g. a stale local file marking
        # a job "new" that Notion already has from a prior run), which is
        # exactly what caused the checkbox count to not match the actual
        # created-row count. Notion's own state can't drift from itself.
        is_new_in_notion = row is None

        try:
            if is_new_in_notion:
                properties = _desired_write_properties(job, company_page_id)
                properties["userDefined:URL"] = url
                properties["date:First Seen:start"] = today
                # Last Seen is written once, here. For a posting that stays
                # open it is then left alone: "was this still up today" is
                # what Still Open answers, and re-stamping Last Seen every
                # run was a full-table write pass buying a value that no
                # view sorts or filters on. It gets one more update, in the
                # close pass, at the moment the posting goes away.
                properties["date:Last Seen:start"] = today
                properties["Still Open"] = "__YES__"
                properties["New This Run"] = "__YES__"
                icon_url = company_icon_by_page_id.get(company_page_id)
                if dry_run:
                    _log_planned_write("(create)", properties, f"URL not yet in Notion: {url}")
                _create_job_posting(properties, icon_url=icon_url, dry_run=dry_run)
                created += 1
            else:
                changed = _changed_job_properties(job, row, company_page_id)
                # Backfill: an existing row with no icon yet gets one copied
                # over from its company, even on a run where nothing else
                # about it changed — otherwise every job created before this
                # was added stays blank forever, since it'll never hit the
                # create branch again.
                icon_url = None if row.get("has_icon") else company_icon_by_page_id.get(company_page_id)
                if not changed and not icon_url:
                    unchanged += 1
                    continue
                if dry_run:
                    _log_planned_write(row["page_id"], changed,
                                       _describe_changes(job, row, changed))
                _update_job_posting(row["page_id"], changed, icon_url=icon_url, dry_run=dry_run)
                updated += 1
        except Exception as e:
            # A single job's write failing (transient Notion outage, rate
            # limit, etc.) should NOT take down the entire sync — and by
            # extension the Excel export and jobs.json write that come
            # after this function returns. Log it, skip it, keep going;
            # this job will just get picked up again next run since its
            # data still lives in output/jobs.json either way.
            print(f"     [sync failed] {job.get('company')} — {job.get('title')}: {e}")
            failed += 1
            failed_jobs.append({"company": job.get("company"), "title": job.get("title"), "url": url})

    # --- Close pass ----------------------------------------------------
    # Absence from this run only proves a posting is gone if we actually
    # scraped the company it belongs to. run_scrapers() catches every
    # scrape exception, so a company that timed out looks identical to a
    # company with nothing open: zero URLs either way. Closing on that
    # basis marked live jobs closed permanently — the next successful run
    # found their URLs already in Notion, took the update branch instead
    # of re-creating them, and left New This Run off, so they never came
    # back to the active views. Classify first and execute second, so the
    # safety valve below can veto the whole pass before any write goes out.
    #
    # This pass is load-bearing for the Scored List view, which filters on
    # Still Open = true. It is deliberately untouched by the delta work
    # above: it was already writing only the rows that move.
    to_close = []
    left_open_failed = []
    left_open_unresolved = []

    for row in existing:
        if not row.get("url") or row["url"] in seen_urls_this_run or not row.get("still_open"):
            continue
        company = row.get("company")
        if company is None:
            # No Company relation at all, or one pointing at a Target List
            # page that no longer exists. Unknown provenance: we cannot
            # confirm this company's scrape succeeded, and defaulting to
            # close on an unconfirmed answer is the original bug. Tracked
            # apart from the failed-scrape bucket because it signals a data
            # problem (an orphaned row) rather than a transient network
            # one, and so wants a human rather than just another run.
            left_open_unresolved.append(row)
        elif company in scraped_ok:
            to_close.append(row)
        else:
            left_open_failed.append(row)

    if dry_run:
        _print_close_candidates(to_close, left_open_failed, left_open_unresolved)

    # Safety valve. One run closing more than a quarter of everything open
    # is a systemic failure — a bad token, a Notion outage, a whole
    # scraper tier broken — not forty roles that all happened to be filled
    # on the same Tuesday. Abort the ENTIRE pass rather than closing a
    # "safe" subset: a partial close still corrupts the dashboard, just
    # more quietly, and nothing here can tell which fraction was real.
    #
    # The floor matters as much as the ratio. On a nearly-empty database
    # every real closure is a large fraction of the total, so without the
    # floor the valve would latch shut and never let anything close again.
    open_count = sum(1 for row in existing if row.get("still_open"))
    limit = open_count * CLOSE_VALVE_MAX_FRACTION
    pct = (100.0 * len(to_close) / open_count) if open_count else 0.0
    over_ratio = bool(open_count) and len(to_close) > limit
    close_aborted = over_ratio and open_count >= CLOSE_VALVE_MIN_OPEN_ROWS

    if close_aborted:
        print(f"     [ABORT] close pass aborted by the safety valve: this run would "
              f"close {len(to_close)} of {open_count} currently-open row(s) = "
              f"{pct:.1f}%, over the "
              f"{CLOSE_VALVE_MAX_FRACTION:.0%} limit ({limit:.1f} row(s)).")
        print("     [ABORT] that is a systemic failure, not real closures — NO rows "
              "were closed. Everything stays open and the next run will retry.")
    elif over_ratio:
        # Loud enough to notice, but explicitly NOT an abort — this is the
        # spurious case the floor exists to wave through, and the log should
        # say so rather than leaving a reader to work out why no abort fired.
        print(f"     [valve] {len(to_close)} of {open_count} open row(s) = {pct:.1f}%, "
              f"over the {CLOSE_VALVE_MAX_FRACTION:.0%} limit — but only {open_count} "
              f"row(s) are open, under the {CLOSE_VALVE_MIN_OPEN_ROWS}-row floor, so "
              f"the ratio does not apply. Closing normally.")

    closed = 0
    if not close_aborted:
        for row in to_close:
            try:
                # This is the one place Last Seen is maintained after
                # creation, and the only moment the pipeline actually
                # learns something new about it: the posting was there
                # last run and is not there now.
                properties = {"Still Open": "__NO__",
                              "date:Last Seen:start": last_seen_on_close}
                # Also reset New This Run — a job that closes while still
                # flagged "new" (e.g. it dropped out of scraping before
                # ever going through a normal update cycle) would otherwise
                # stay stuck showing as new forever, since nothing ever
                # touches a closed job again after this point. Only when
                # it is actually set, so a closing row that was never
                # flagged doesn't carry a redundant property.
                if row.get("new_this_run"):
                    properties["New This Run"] = "__NO__"
                if dry_run:
                    _log_planned_write(row["page_id"], properties,
                                       f"closing: URL absent this run, "
                                       f"{row.get('company')} scraped OK")
                _update_job_posting(row["page_id"], properties, dry_run=dry_run)
                closed += 1
            except Exception as e:
                print(f"     [sync failed] closing {row.get('url')}: {e}")

    if left_open_failed:
        failed_companies = {row["company"] for row in left_open_failed}
        print(f"     {len(left_open_failed)} job(s) from {len(failed_companies)} failed "
              f"companies left open rather than closed")
    if left_open_unresolved:
        print(f"     {len(left_open_unresolved)} job(s) with an unresolved company "
              f"relation left open — check these manually")

    counts = {"created": created, "updated": updated, "unchanged": unchanged,
              "closed": closed, "failed": failed}
    # Written last, and only on a real run: a rehearsal that advanced
    # last_run would make the next real run stamp closing postings with the
    # date of a run that never happened.
    state.save(rubric_fingerprint, counts=counts, dry_run=dry_run)

    return {"created": created, "updated": updated, "unchanged": unchanged,
            "closed": closed, "failed": failed, "failed_jobs": failed_jobs,
            "left_open": len(left_open_failed),
            "left_open_unresolved": len(left_open_unresolved),
            "close_aborted": close_aborted,
            "date_applied_backfilled": date_applied_backfilled}


def _print_close_candidates(to_close, left_open_failed, left_open_unresolved):
    """Dry-run view of the close pass: every open row whose URL did not turn
    up this run, and the reason it would or would not be closed. This is the
    output to eyeball before letting a real run touch Still Open."""
    total = len(to_close) + len(left_open_failed) + len(left_open_unresolved)
    if not total:
        print("     [dry-run] every open row showed up this run — nothing to close")
        return

    print(f"     [dry-run] close candidates ({total} open row(s) whose URL did not "
          f"appear this run):")
    rows = ([(row, "CLOSE     ", "scraped OK") for row in to_close]
            + [(row, "leave open", "scrape FAILED this run") for row in left_open_failed]
            + [(row, "leave open", "no/unresolved Company relation")
               for row in left_open_unresolved])
    for row, verdict, reason in rows:
        company = row.get("company") or "(unresolved company)"
        title = row.get("title") or "(untitled)"
        print(f"       {verdict} {company[:30]:<30} — {reason:<30} {title[:50]}")


def _query_all_job_postings(company_name_by_page_id=None):
    """Read every Job Postings row.

    company_name_by_page_id maps Target List page ids to company names;
    pass it and each row comes back with a resolved "company". The close
    pass needs to know which company a row belongs to before it can decide
    whether that company's scrape succeeded, and it matches on the
    relation's page id rather than on name strings so a company renamed in
    Notion cannot silently orphan its own postings. Left None, "company"
    comes back None on every row, which the close pass reads as unknown
    provenance and never closes.

    The lookup is passed in rather than fetched here because
    sync_jobs_to_notion() already queries the Target List for the reverse
    map — re-querying would add a fourth full table scan to a run that
    already does too many (punch list #12)."""
    rows, cursor = [], None
    while True:
        url = f"{BASE_URL}/databases/{JOB_POSTINGS_DATABASE_ID}/query"
        payload = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        _count("read:job_postings_page")
        resp = session.post(url, headers=HEADERS, json=payload)
        _raise_with_detail(resp)
        data = resp.json()
        for page in data["results"]:
            props = page["properties"]
            relation = (props.get("Company") or {}).get("relation") or []
            company_page_id = relation[0]["id"] if relation else None
            rows.append({
                "page_id": page["id"],
                "url": (props.get("URL") or {}).get("url"),
                "still_open": (props.get("Still Open") or {}).get("checkbox", False),
                "title": _plain_text(props.get("Job Title")),
                "company_page_id": company_page_id,
                "company": (company_name_by_page_id or {}).get(company_page_id),
                "has_icon": page.get("icon") is not None,
                # Everything below is here so the sync can diff a row
                # against what Notion already holds and skip writing when
                # nothing moved. These come out of the same response as
                # the fields above, so reading them costs no extra
                # request — the sync used to rewrite all of them on every
                # row every run purely because it had never read them
                # back and so had nothing to compare against.
                "score": (props.get("Score") or {}).get("number"),
                "routing": _select_name(props.get("Routing")),
                "reasoning": _plain_text(props.get("Reasoning")),
                "ambiguity_note": _plain_text(props.get("Ambiguity Note")),
                "ic_role": (props.get("IC Role") or {}).get("checkbox", False),
                "location": _plain_text(props.get("Location")),
                "ats": _plain_text(props.get("ATS")),
                "new_this_run": (props.get("New This Run") or {}).get("checkbox", False),
                "last_seen": _date_start(props.get("Last Seen")),
                "application_status": _select_name(props.get("Application Status")),
                "date_applied": _date_start(props.get("Date Applied")),
            })
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    return rows


def _backfill_applied_dates(existing_rows, today, dry_run=False):
    """Application Status is set by hand in Notion. Stamping Date Applied
    the instant that happens is a real Notion database automation, but
    those need a paid Notion plan — this is the free substitute. Once a
    run notices Application Status = "Applied" with Date Applied still
    blank, it fills in today's date.

    Runs against every row Notion currently holds (existing_rows, from
    _query_all_job_postings), not just this run's scrape — so it still
    catches a posting marked Applied after it has already closed and
    stopped showing up in the scrape, which the main create/update loop
    above would otherwise never revisit.

    Only ever fills a blank Date Applied. A row already carrying a date —
    whether typed in by hand or stamped by a previous run — is left
    alone, and no other Application Status value is touched."""
    backfilled = 0
    for row in existing_rows:
        if row.get("application_status") != "Applied" or row.get("date_applied"):
            continue
        if dry_run:
            print(f"     [backfill] [dry-run] would stamp Date Applied={today} on "
                  f"{row.get('company')} — {row.get('title')}")
            backfilled += 1
            continue
        try:
            _update_job_posting(row["page_id"], {"date:Date Applied:start": today})
            backfilled += 1
        except Exception as e:
            print(f"     [backfill failed] {row.get('company')} — {row.get('title')}: {e}")
    return backfilled


def _query_all_target_list_pages():
    rows, cursor = [], None
    while True:
        url = f"{BASE_URL}/databases/{NOTION_DATABASE_ID}/query"
        payload = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        _count("read:target_list_page")
        resp = session.post(url, headers=HEADERS, json=payload)
        _raise_with_detail(resp)
        data = resp.json()
        for page in data["results"]:
            # icon_url carries the company's own page icon (set by
            # update_company_website()'s favicon stamp) so callers can copy
            # it onto related rows elsewhere — e.g. Job Postings, which has
            # no icon-setting logic of its own. Only "external" icons have a
            # copyable URL; an emoji icon (like BLANK_ICON) or a Notion-
            # hosted "file" icon yields None here and is simply skipped by
            # the caller.
            icon = page.get("icon")
            icon_url = icon["external"]["url"] if icon and icon.get("type") == "external" else None
            rows.append({
                "page_id": page["id"],
                "company": _plain_text(page["properties"].get("Company")),
                "icon_url": icon_url,
            })
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    return rows


def _create_job_posting(properties, icon_url=None, dry_run=False):
    _count("write:job_create")
    if dry_run:
        # Must return before the throttle sleep below — a dry run over
        # 1,000+ jobs sleeping 0.35s each for a write that never happens
        # would sit there for minutes doing nothing.
        return None
    url = f"{BASE_URL}/pages"
    payload = {"parent": {"database_id": JOB_POSTINGS_DATABASE_ID},
               "properties": _build_job_properties(properties)}
    if icon_url:
        payload["icon"] = {"type": "external", "external": {"url": icon_url}}
    resp = session.post(url, headers=HEADERS, json=payload)
    _raise_with_detail(resp)
    time.sleep(0.35)  # stay under Notion's ~3 req/s limit instead of retrying 429s after the fact
    return resp.json()


def _update_job_posting(page_id, properties, icon_url=None, dry_run=False):
    _count("write:job_update")
    if dry_run:
        return None
    url = f"{BASE_URL}/pages/{page_id}"
    payload = {"properties": _build_job_properties(properties)}
    if icon_url:
        payload["icon"] = {"type": "external", "external": {"url": icon_url}}
    resp = session.patch(url, headers=HEADERS, json=payload)
    _raise_with_detail(resp)
    time.sleep(0.35)
    return resp.json()


def _build_job_properties(properties):
    """Translate a flat {field_name: value} dict into Notion's typed
    property format, per the Job Postings schema."""
    out = {}
    for key, value in properties.items():
        if value is None:
            continue
        if key == "title":
            out["Job Title"] = {"title": [{"text": {"content": str(value)[:2000]}}]}
        elif key == "Company":
            out["Company"] = {"relation": [{"id": pid} for pid in value]}
        elif key == "Score":
            out["Score"] = {"number": value}
        elif key == "Routing":
            out["Routing"] = {"select": {"name": value}} if value else {"select": None}
        elif key in ("Reasoning", "Ambiguity Note", "Location", "ATS"):
            out[key] = {"rich_text": [{"text": {"content": str(value)[:2000]}}] if value else []}
        elif key == "IC Role":
            out["IC Role"] = {"checkbox": value == "__YES__"}
        elif key == "Still Open":
            out["Still Open"] = {"checkbox": value == "__YES__"}
        elif key == "New This Run":
            out["New This Run"] = {"checkbox": value == "__YES__"}
        elif key == "userDefined:URL":
            out["URL"] = {"url": value or None}
        elif key.startswith("date:"):
            _, field, part = key.split(":")
            out.setdefault(field, {"date": {}})
            out[field]["date"]["start" if part == "start" else "end"] = value
    return out


def _plain_text(prop):
    if not prop:
        return None
    if prop.get("type") == "title":
        parts = prop.get("title", [])
    elif prop.get("type") == "rich_text":
        parts = prop.get("rich_text", [])
    else:
        return None
    return "".join(p.get("plain_text", "") for p in parts) or None


def _url_value(prop):
    if not prop or prop.get("type") != "url":
        return None
    return prop.get("url") or None


def _select_name(prop):
    """A select that has never been set comes back as
    {"type": "select", "select": None} rather than as a missing key, so the
    None check has to happen after the type check rather than instead of it."""
    if not prop or prop.get("type") != "select":
        return None
    selected = prop.get("select")
    return selected.get("name") if selected else None


def _date_start(prop):
    """Only the start of a date property. Nothing here writes ranges, and if
    something ever did, an ignored end would compare unequal every run and
    rewrite the row forever."""
    if not prop or prop.get("type") != "date":
        return None
    value = prop.get("date")
    return value.get("start") if value else None

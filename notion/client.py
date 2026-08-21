"""
Thin wrapper around the Notion API for the Target List database.

Requires env var NOTION_API_KEY (a Notion internal integration token,
created at https://www.notion.so/my-integrations, then shared with the
Target List database via "Connections" in Notion's UI).

Requires env var NOTION_DATABASE_ID (the ID of the Target List database,
found in its page URL).
"""
import os
import time
import requests

NOTION_API_KEY = os.environ["NOTION_API_KEY"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
JOB_POSTINGS_DATABASE_ID = os.environ["JOB_POSTINGS_DATABASE_ID"]
NOTION_VERSION = "2022-06-28"

BASE_URL = "https://api.notion.com/v1"
HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
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
    [{page_id, company, sector, hq, source, ats_platform, scrape_method}, ...]
    """
    rows = []
    payload = {"page_size": 100}
    url = f"{BASE_URL}/databases/{NOTION_DATABASE_ID}/query"

    while True:
        resp = requests.post(url, headers=HEADERS, json=payload)
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


def update_company_info(page_id, sector=None, hq=None, source=None,
                          ats_platform=None, scrape_method=None, careers_url=None):
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

    payload = {"properties": properties}
    resp = requests.patch(url, headers=HEADERS, json=payload)
    _raise_with_detail(resp)
    return resp.json()


def update_ats_fields(page_id, ats_platform, scrape_method, careers_url=None):
    """Kept for backward compatibility — writes just the ATS-related fields."""
    return update_company_info(
        page_id, ats_platform=ats_platform, scrape_method=scrape_method, careers_url=careers_url
    )


def sync_jobs_to_notion(jobs):
    """Write scored jobs to the Job Postings Notion database, deduped by URL.

    - New URL -> create a row, First Seen = today, linked to the matching
      Target List company via the Company relation.
    - Known URL -> update Score/Reasoning/Routing/Last Seen on the existing
      row, never duplicate it.
    - Anything previously in Job Postings that did NOT show up in this
      run's scrape gets Still Open flipped to false (not deleted — the
      history stays, it's just marked closed).
    """
    from datetime import date
    today = date.today().isoformat()

    existing = _query_all_job_postings()
    existing_by_url = {row["url"]: row for row in existing if row.get("url")}

    company_pages = _query_all_target_list_pages()
    company_page_by_name = {row["company"]: row["page_id"] for row in company_pages}

    seen_urls_this_run = set()
    created, updated, failed = 0, 0, 0
    failed_jobs = []

    for job in jobs:
        url = job.get("url")
        if not url:
            continue  # can't dedup or relate without a URL, skip rather than risk a duplicate
        seen_urls_this_run.add(url)

        company_page_id = company_page_by_name.get(job.get("company"))
        is_new_in_notion = url not in existing_by_url  # Notion's own, authoritative answer —
        # this is what "New This Run" is now based on, instead of the separate
        # local seen_jobs.json file. Using two independent sources for the same
        # question let them drift out of sync (e.g. a stale local file marking
        # a job "new" that Notion already has from a prior run), which is
        # exactly what caused the checkbox count to not match the actual
        # created-row count. Notion's own state can't drift from itself.
        properties = {
            "title": job.get("title") or "(untitled)",
            "Score": job.get("score"),
            "Routing": job.get("routing"),
            "Reasoning": job.get("reasoning"),
            "Ambiguity Note": job.get("ambiguity_note") or "",
            "IC Role": "__YES__" if job.get("ic_role_flag") else "__NO__",
            "Location": job.get("location") or "",
            "ATS": job.get("source_ats") or "",
            "userDefined:URL": url,
            "date:Last Seen:start": today,
            "Still Open": "__YES__",
            "New This Run": "__YES__" if is_new_in_notion else "__NO__",
        }
        if company_page_id:
            properties["Company"] = [company_page_id]

        try:
            if not is_new_in_notion:
                _write_with_retry(_update_job_posting, existing_by_url[url]["page_id"], properties)
                updated += 1
            else:
                properties["date:First Seen:start"] = today
                _write_with_retry(_create_job_posting, properties)
                created += 1
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

    closed = 0
    for row in existing:
        if row.get("url") and row["url"] not in seen_urls_this_run and row.get("still_open"):
            try:
                # Also reset New This Run to false — a job that closes while
                # still flagged "new" (e.g. it dropped out of scraping before
                # ever going through a normal update cycle) would otherwise
                # stay stuck showing as new forever, since nothing ever
                # touches a closed job again after this point.
                _write_with_retry(_update_job_posting, row["page_id"],
                                    {"Still Open": "__NO__", "New This Run": "__NO__"})
                closed += 1
            except Exception as e:
                print(f"     [sync failed] closing {row.get('url')}: {e}")

    return {"created": created, "updated": updated, "closed": closed, "failed": failed, "failed_jobs": failed_jobs}


def _write_with_retry(fn, *args, max_attempts=3, **kwargs):
    """Retry a Notion write a couple times with a short backoff before
    giving up — a 504 gateway timeout or a rate limit is often transient
    and succeeds on the very next attempt seconds later, so it's worth
    trying again before treating it as a real failure."""
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_error = e
            if attempt < max_attempts:
                time.sleep(2 * attempt)  # 2s, then 4s
    raise last_error


def _query_all_job_postings():
    rows, cursor = [], None
    while True:
        url = f"{BASE_URL}/databases/{JOB_POSTINGS_DATABASE_ID}/query"
        payload = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        resp = requests.post(url, headers=HEADERS, json=payload)
        _raise_with_detail(resp)
        data = resp.json()
        for page in data["results"]:
            props = page["properties"]
            rows.append({
                "page_id": page["id"],
                "url": (props.get("URL") or {}).get("url"),
                "still_open": (props.get("Still Open") or {}).get("checkbox", False),
            })
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    return rows


def _query_all_target_list_pages():
    rows, cursor = [], None
    while True:
        url = f"{BASE_URL}/databases/{NOTION_DATABASE_ID}/query"
        payload = {"page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        resp = requests.post(url, headers=HEADERS, json=payload)
        _raise_with_detail(resp)
        data = resp.json()
        for page in data["results"]:
            rows.append({"page_id": page["id"], "company": _plain_text(page["properties"].get("Company"))})
        if not data.get("has_more"):
            break
        cursor = data["next_cursor"]
    return rows


def _create_job_posting(properties):
    url = f"{BASE_URL}/pages"
    payload = {"parent": {"database_id": JOB_POSTINGS_DATABASE_ID},
               "properties": _build_job_properties(properties)}
    resp = requests.post(url, headers=HEADERS, json=payload)
    _raise_with_detail(resp)
    return resp.json()


def _update_job_posting(page_id, properties):
    url = f"{BASE_URL}/pages/{page_id}"
    payload = {"properties": _build_job_properties(properties)}
    resp = requests.patch(url, headers=HEADERS, json=payload)
    _raise_with_detail(resp)
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

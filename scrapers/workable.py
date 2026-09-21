"""Pulls open job postings from a Workable job board.

Workable is the largest ATS in the target list with no scraper — 13
companies, more than Lever and Workday each. Every one of them was either
falling through to scrapers/html_generic.py (which sees a JS-rendered
shell and reports zero) or, for the rows marked "API", getting no scrape
attempt at all.

The public board a human sees at

    https://apply.workable.com/{slug}/

is backed by an unauthenticated JSON endpoint:

    GET https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true

`details=true` is not optional here: without it the response carries the
job list but no `description` field, and the scorer would see every
Workable posting as "not available". The whole board comes back in one
response — there is no cursor, offset or `total` in the payload to page
with — so a single GET is the complete answer, confirmed against the
largest board in the list (ARCHIMED, 23 postings).

A slug with no open roles answers 200 with `"jobs": []` rather than 404
(CVRx, Kneu, Medcura and Shockwave Medical all do today). That is a real
"nothing open", not a failure, and returning [] lets the close pass retire
their stale rows correctly. A *wrong* slug 404s, which raise_for_status
turns into the failure it is.
"""
from http_client import session

from ats_finder.find_ats import slugify


def _location(job):
    """Build a human-readable location from Workable's split fields.

    Workable gives city/state/country as separate keys rather than one
    string, and any of them can be "" on a remote or unspecified posting.
    `telecommuting` is the only signal for a fully-remote role, so an
    otherwise-empty location falls back to "Remote" instead of None —
    "Remote" is information the scorer uses, None is not.
    """
    parts = [job.get("city"), job.get("state"), job.get("country")]
    text = ", ".join(p for p in parts if p)
    if text:
        return f"{text} (remote)" if job.get("telecommuting") else text
    return "Remote" if job.get("telecommuting") else None


def fetch_jobs(company_name, slug=None):
    slug = slug or slugify(company_name)
    url = f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true"
    resp = session.get(url, timeout=15)
    resp.raise_for_status()
    jobs = resp.json().get("jobs", [])

    return [
        {
            "company": company_name,
            "title": job["title"],
            "location": _location(job),
            # `url` and `shortlink` are the same apply.workable.com/j/{code}
            # link today, but only `shortlink` is documented as stable, so
            # it is the fallback rather than the other way round.
            "url": job.get("url") or job.get("shortlink"),
            "posted_at": job.get("published_on") or job.get("created_at"),
            "description": job.get("description"),  # HTML; scoring cleans it
            "source_ats": "Workable",
        }
        for job in jobs
    ]

"""Pulls open job postings from a Breezy HR job board.

The board a human sees at

    https://{slug}.breezy.hr/

serves its full posting list as JSON from the same host:

    GET https://{slug}.breezy.hr/json

The response is a bare JSON array, not an object with a "jobs" key, so
there is nothing to unwrap and nothing to page — the whole board is the
document.

A board that has been closed or renamed answers 404 with an HTML error
page rather than an empty array (BrainsWay's `brainsway` board does this
today). raise_for_status turns that into the failure it is, which is
correct: an unreachable board is not evidence that a company has no open
roles, and treating it as such would close every one of its rows.
"""
from http_client import session

from ats_finder.find_ats import slugify


def _location(job):
    """Breezy nests location under an object that may describe a remote
    role with no place name at all. `is_remote` is the only signal for
    those, so it is used to produce "Remote" rather than a None the
    scorer would read as unknown."""
    loc = job.get("location") or {}
    name = (loc.get("name") or "").strip()
    if name:
        return f"{name} (remote)" if loc.get("is_remote") else name
    return "Remote" if loc.get("is_remote") else None


def fetch_jobs(company_name, slug=None):
    slug = slug or slugify(company_name)
    resp = session.get(f"https://{slug}.breezy.hr/json", timeout=15)
    resp.raise_for_status()
    jobs = resp.json()

    return [
        {
            "company": company_name,
            "title": job["name"],
            "location": _location(job),
            "url": job.get("url"),
            "posted_at": job.get("published_date"),
            # The board list carries no description. Breezy exposes one
            # per posting, but that is a round trip per job for a field
            # the scorer already treats as optional — the same trade-off
            # scrapers/workday.py documents, and the same answer.
            "description": None,
            "source_ats": "Breezy HR",
        }
        for job in jobs
    ]

"""Pulls open job postings from a Rippling ATS job board.

The board a human sees at

    https://ats.rippling.com/{slug}/jobs

is backed by an unauthenticated JSON API:

    GET https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs
    GET https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs/{uuid}

The list response is a bare array and carries title, department, location
and a public URL — but no description. Unlike Workday (see that module's
note on the same trade-off) the detail call is worth making here: Rippling
boards in this list are small (single digits), so the cost is a handful of
requests, and the description is what lets a posting be scored on more
than its title.

MAX_DETAIL_FETCHES is the guard that keeps that judgement from silently
inverting if a large employer ever lands on Rippling. Past the cap the
remaining postings are still returned — with title, location and URL, just
no description — rather than dropped. A partial answer is right; a board
that quietly takes 500 round trips is not, and neither is losing postings.
"""
from http_client import session

from ats_finder.find_ats import slugify

API = "https://api.rippling.com/platform/api/ats/v1/board"

# Roughly ten times the largest Rippling board in the target list today.
MAX_DETAIL_FETCHES = 100


def _location_from_list(job):
    return ((job.get("workLocation") or {}).get("label") or "").strip() or None


def _description(detail):
    """Rippling splits the body into {"company": ..., "role": ...}.

    Role first: it is the part that describes the job, and _clean_description
    in scoring truncates, so leading with the boilerplate company blurb
    would push the actual responsibilities out of the prompt on a long
    posting.
    """
    desc = detail.get("description")
    if isinstance(desc, dict):
        parts = [desc.get("role"), desc.get("company")]
        return "\n".join(p for p in parts if p) or None
    return desc or None


def fetch_jobs(company_name, slug=None):
    slug = slug or slugify(company_name)
    resp = session.get(f"{API}/{slug}/jobs", timeout=15)
    resp.raise_for_status()
    listing = resp.json()

    jobs = []
    for i, job in enumerate(listing):
        uuid = job.get("uuid")
        description = None
        posted_at = None
        location = _location_from_list(job)

        if uuid and i < MAX_DETAIL_FETCHES:
            try:
                detail = session.get(f"{API}/{slug}/jobs/{uuid}", timeout=15)
                detail.raise_for_status()
                detail = detail.json()
            except Exception:
                # One posting's detail call failing is not the board
                # failing. Keep the row with what the list already gave us
                # rather than raising, which would discard every posting
                # from this company and — via scraped_ok — make the run
                # unable to tell "gone" from "never fetched".
                detail = None
            if detail:
                description = _description(detail)
                posted_at = detail.get("createdOn")
                locations = detail.get("workLocations") or []
                if locations:
                    location = ", ".join(str(loc) for loc in locations)

        jobs.append({
            "company": company_name,
            "title": job.get("name"),
            "location": location,
            "url": job.get("url") or f"https://ats.rippling.com/{slug}/jobs/{uuid}",
            "posted_at": posted_at,
            "description": description,
            "source_ats": "Rippling",
        })

    return jobs

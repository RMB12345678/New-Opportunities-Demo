"""Pulls open job postings from an Ashby job board."""
from urllib.parse import quote

from http_client import session

from ats_finder.find_ats import slugify


def fetch_jobs(company_name, slug=None):
    """Fetch a board by its Ashby slug.

    The slug is percent-encoded because Ashby slugs are not always the
    lowercase-no-space token every other board uses: Redesign Health's is
    literally "Redesign Health", space and capitals included. Sending that
    raw in the path 404s, so the encoding happens here rather than asking
    every caller to remember it.
    """
    slug = slug or slugify(company_name)
    url = f"https://api.ashbyhq.com/posting-api/job-board/{quote(slug)}"
    resp = session.get(url, timeout=10)
    resp.raise_for_status()
    jobs = resp.json().get("jobs", [])

    return [
        {
            "company": company_name,
            "title": job["title"],
            "location": job.get("location"),
            "url": job.get("jobUrl"),
            "posted_at": job.get("publishedAt"),
            "description": job.get("descriptionPlain"),
            "source_ats": "Ashby",
        }
        for job in jobs
    ]

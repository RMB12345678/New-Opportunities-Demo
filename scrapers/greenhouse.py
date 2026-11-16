"""Pulls open job postings from a Greenhouse job board."""
from http_client import session
from scrapers.common import require_list

from ats_finder.find_ats import slugify


def fetch_jobs(company_name, slug=None):
    slug = slug or slugify(company_name)
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    resp = session.get(url, timeout=10)
    resp.raise_for_status()
    jobs = require_list(resp.json(), "jobs", "Greenhouse")

    return [
        {
            "company": company_name,
            "title": job["title"],
            "location": (job.get("location") or {}).get("name"),
            "url": job.get("absolute_url"),
            "posted_at": job.get("updated_at"),
            "description": job.get("content"),  # HTML-escaped HTML; scoring cleans it
            "source_ats": "Greenhouse",
        }
        for job in jobs
    ]

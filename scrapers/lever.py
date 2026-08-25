"""Pulls open job postings from a Lever job board."""
from http_client import session

from ats_finder.find_ats import slugify


def fetch_jobs(company_name, slug=None):
    slug = slug or slugify(company_name)
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    resp = session.get(url, timeout=10)
    resp.raise_for_status()
    jobs = resp.json()

    return [
        {
            "company": company_name,
            "title": job["text"],
            "location": (job.get("categories") or {}).get("location"),
            "url": job.get("hostedUrl"),
            "posted_at": job.get("createdAt"),
            "description": job.get("descriptionPlain"),
            "source_ats": "Lever",
        }
        for job in jobs
    ]

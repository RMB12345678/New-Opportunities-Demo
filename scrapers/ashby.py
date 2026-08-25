"""Pulls open job postings from an Ashby job board."""
from http_client import session

from ats_finder.find_ats import slugify


def fetch_jobs(company_name, slug=None):
    slug = slug or slugify(company_name)
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
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

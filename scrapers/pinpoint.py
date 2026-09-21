"""Pulls open job postings from a Pinpoint (pinpointhq.com) job board.

The board a human sees at

    https://{slug}.pinpointhq.com/

serves its postings as JSON from the same host:

    GET https://{slug}.pinpointhq.com/postings.json

The payload wraps the list in a "data" key. Unusually for the boards here
it carries the full posting body inline, split across three HTML fields
(description, key_responsibilities, skills_knowledge_expertise), so no
per-posting round trip is needed to give the scorer real text.

It carries no publication date — only `deadline_at`, which is when the
posting closes, not when it opened. posted_at is left None rather than
filled with a deadline that would read as a start date and be wrong in
both direction and meaning.
"""
from http_client import session

from ats_finder.find_ats import slugify


def _location(job):
    """Pinpoint's location object has both `name` (a display label, e.g.
    "Frankfurt") and `city` (the full form, "Frankfurt am Main"). `name`
    is what the board itself shows, so it wins; `city` is the fallback for
    boards that leave the label blank."""
    loc = job.get("location") or {}
    label = (loc.get("name") or loc.get("city") or "").strip()
    workplace = (job.get("workplace_type_text") or "").strip()
    if label:
        return f"{label} ({workplace})" if workplace else label
    return workplace or None


def _description(job):
    """Join the three body fields the board renders as one posting.

    Kept in the order Pinpoint displays them, with the publisher's own
    section headers where they set one, because the headers are part of
    how seniority and scope get described.
    """
    parts = []
    for header_key, body_key in (
        (None, "description"),
        ("key_responsibilities_header", "key_responsibilities"),
        ("skills_knowledge_expertise_header", "skills_knowledge_expertise"),
        ("benefits_header", "benefits"),
    ):
        body = (job.get(body_key) or "").strip()
        if not body:
            continue
        header = (job.get(header_key) or "").strip() if header_key else ""
        parts.append(f"<h3>{header}</h3>{body}" if header else body)
    return "\n".join(parts) or None


def fetch_jobs(company_name, slug=None):
    slug = slug or slugify(company_name)
    resp = session.get(f"https://{slug}.pinpointhq.com/postings.json", timeout=15)
    resp.raise_for_status()
    jobs = resp.json().get("data", [])

    return [
        {
            "company": company_name,
            "title": job["title"],
            "location": _location(job),
            "url": job.get("url"),
            "posted_at": None,  # see module docstring: only a deadline is published
            "description": _description(job),
            "source_ats": "Pinpoint",
        }
        for job in jobs
    ]

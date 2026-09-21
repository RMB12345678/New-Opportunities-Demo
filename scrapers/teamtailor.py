"""Pulls open job postings from a Teamtailor career site.

Teamtailor's documented API needs an API token per employer, which is not
something a public scraper can have. Every career site, however, publishes
a JSON Feed (jsonfeed.org) of its open roles at

    GET https://{host}/jobs.json

and that is unauthenticated. Confirmed against both companies in the
target list.

The awkward part is `{host}`. Teamtailor's other customers here do not sit
on a shared domain the way Greenhouse and Lever boards do — Nordic Capital
is at career.nordiccapital.com and Flow Neuroscience at
careers.flowneuroscience.com. So the "slug" this scraper takes is a
hostname, not a board name, and run.py's SLUG_URL_PATTERNS entry for
Teamtailor is written to capture one. The fallback when no host can be
read from the saved Careers URL is {slug}.teamtailor.com, Teamtailor's own
namespace, which is safe to guess for the same reason a Greenhouse slug
is: a wrong guess 404s inside the vendor's namespace rather than resolving
to some other company's board.
"""
from http_client import session

from ats_finder.find_ats import slugify


def _location(item):
    """Pull a place name out of the schema.org JobPosting the feed embeds.

    The JSON Feed's own fields carry no location at all — it is only in
    `_jobposting`, Teamtailor's extension holding the same posting as
    schema.org markup. jobLocation is a list (a role can be advertised in
    several cities), so all of them are joined rather than silently
    reporting the first, which would make a three-city role look local.
    """
    posting = item.get("_jobposting")
    if not isinstance(posting, dict):
        return None

    places = posting.get("jobLocation") or []
    if isinstance(places, dict):
        places = [places]

    names = []
    for place in places:
        address = (place or {}).get("address") or {}
        parts = [address.get("addressLocality"), address.get("addressCountry")]
        text = ", ".join(p for p in parts if p)
        if text and text not in names:
            names.append(text)
    return " / ".join(names) or None


def fetch_jobs(company_name, slug=None):
    host = slug or f"{slugify(company_name)}.teamtailor.com"
    resp = session.get(f"https://{host}/jobs.json", timeout=15)
    resp.raise_for_status()
    items = resp.json().get("items", [])

    return [
        {
            "company": company_name,
            "title": item.get("title"),
            "location": _location(item),
            "url": item.get("url"),
            "posted_at": item.get("date_published"),
            "description": item.get("content_html"),  # HTML; scoring cleans it
            "source_ats": "Teamtailor",
        }
        for item in items
    ]

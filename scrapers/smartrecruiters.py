"""Pulls open job postings from a SmartRecruiters job board.

The one platform here with a genuinely documented, public, unauthenticated
API:

    GET https://api.smartrecruiters.com/v1/companies/{slug}/postings
    GET https://api.smartrecruiters.com/v1/companies/{slug}/postings/{id}

Two things the list response does not give you, both of which the detail
call does:

  - the description (the body lives in jobAd.sections), and
  - a public posting URL. The list carries `ref`, which is the API URL for
    the posting, not a page a human can open. Writing that into Notion
    would produce a dashboard full of links to JSON.

So the detail call is made per posting, capped by MAX_DETAIL_FETCHES the
same way scrapers/rippling.py caps its own — SmartRecruiters is one
company in this list, but the API is also what large employers use, and an
uncapped round trip per posting is how a 2,000-role board quietly becomes
the slowest step in the run. Past the cap a posting still comes back, with
its URL built from the documented jobs.smartrecruiters.com form.

Unlike the other boards here the list IS paged — `totalFound` and `offset`
are real, and the server caps `limit` at 100 — so a board of 250 that
returns 100 looks exactly like a small company unless the loop is right.
"""
from http_client import session

from ats_finder.find_ats import slugify

API = "https://api.smartrecruiters.com/v1/companies"

PAGE_SIZE = 100          # the server's documented maximum
MAX_PAGES = 100          # 10,000 postings; a runaway guard, not a real limit
MAX_DETAIL_FETCHES = 100


def _location(posting):
    loc = posting.get("location") or {}
    full = (loc.get("fullLocation") or "").strip()
    if full:
        return full
    parts = [loc.get("city"), loc.get("region"), loc.get("country")]
    text = ", ".join(p for p in parts if p)
    if text:
        return text
    return "Remote" if loc.get("remote") else None


def _description(detail):
    """Join the jobAd sections SmartRecruiters splits a posting into.

    Job description and qualifications first: companyDescription is
    boilerplate that would otherwise consume the scorer's truncation
    budget ahead of the part that describes the actual role.
    """
    sections = ((detail.get("jobAd") or {}).get("sections") or {})
    parts = []
    for key in ("jobDescription", "qualifications", "companyDescription",
                "additionalInformation"):
        section = sections.get(key) or {}
        text = (section.get("text") or "").strip()
        if not text:
            continue
        title = (section.get("title") or "").strip()
        parts.append(f"<h3>{title}</h3>{text}" if title else text)
    return "\n".join(parts) or None


def _fetch_all_postings(slug):
    postings = []
    offset = 0
    for _ in range(MAX_PAGES):
        resp = session.get(
            f"{API}/{slug}/postings",
            params={"limit": PAGE_SIZE, "offset": offset},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        page = data.get("content") or []
        postings.extend(page)
        offset += len(page)
        # Stop on a short page as well as on the count: a board whose
        # totalFound disagrees with what it actually serves would
        # otherwise spin until MAX_PAGES.
        if not page or offset >= data.get("totalFound", 0):
            break
    return postings


def fetch_jobs(company_name, slug=None):
    slug = slug or slugify(company_name)
    postings = _fetch_all_postings(slug)

    jobs = []
    for i, posting in enumerate(postings):
        posting_id = posting.get("id")
        fallback_url = f"https://jobs.smartrecruiters.com/{slug}/{posting_id}"
        description = None
        url = fallback_url

        if posting_id and i < MAX_DETAIL_FETCHES:
            try:
                detail = session.get(f"{API}/{slug}/postings/{posting_id}", timeout=20)
                detail.raise_for_status()
                detail = detail.json()
            except Exception:
                # Same reasoning as scrapers/rippling.py: one posting's
                # detail failing is not the board failing, and raising
                # here would discard every posting for this company.
                detail = None
            if detail:
                description = _description(detail)
                url = detail.get("postingUrl") or detail.get("applyUrl") or fallback_url

        jobs.append({
            "company": company_name,
            "title": (posting.get("name") or "").strip(),
            "location": _location(posting),
            "url": url,
            "posted_at": posting.get("releasedDate"),
            "description": description,
            "source_ats": "SmartRecruiters",
        })

    return jobs

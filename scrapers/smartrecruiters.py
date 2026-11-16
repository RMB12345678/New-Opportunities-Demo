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
the slowest step in the run. The cap is far above any real board and a board
over it raises: the URL is a posting's identity, so there is deliberately no
second, bare-id URL form for postings past the cap or for a failed detail call.

Unlike the other boards here the list IS paged — `totalFound` and `offset`
are real, and the server caps `limit` at 100 — so a board of 250 that
returns 100 looks exactly like a small company unless the loop is right.
"""
from http_client import session

from ats_finder.find_ats import slugify
from scrapers.common import ScrapeShapeError

API = "https://api.smartrecruiters.com/v1/companies"

PAGE_SIZE = 100          # the server's documented maximum
MAX_PAGES = 100          # 10,000 postings; a runaway guard, not a real limit
# Far above any real board (Brainlab has ~126). A posting's URL must come from
# the same source on every run, because the URL is its identity (invariant 20):
# with a cap near the board size, postings crossing it swapped postingUrl for a
# bare-id fallback and their rows closed and re-created. A board over the cap
# raises rather than emitting a second identity for the overflow.
MAX_DETAIL_FETCHES = 2000


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
    if len(postings) > MAX_DETAIL_FETCHES:
        raise ScrapeShapeError(
            f"SmartRecruiters board '{slug}' has {len(postings)} postings, over the "
            f"{MAX_DETAIL_FETCHES}-detail cap; postings past it would get a different "
            f"URL (identity) than the rest, so the board is not scraped at all")

    jobs = []
    for posting in postings:
        posting_id = posting.get("id")
        description = None
        # The detail call is the ONLY source of the URL. Unlike Rippling,
        # whose URL comes from the list, a failed detail call here cannot
        # degrade to a lesser posting: the bare-id fallback is a second
        # identity for the same posting (the old row closes, a new one is
        # created as New This Run). So a failure raises for the whole
        # company, which lands in Failed with its rows protected
        # (invariant 2).
        if not posting_id:
            continue  # nothing to build an identity from
        try:
            detail = session.get(f"{API}/{slug}/postings/{posting_id}", timeout=20)
            detail.raise_for_status()
            detail = detail.json()
        except Exception as e:
            raise RuntimeError(
                f"SmartRecruiters detail call for posting {posting_id} on '{slug}' "
                f"failed ({e}); not falling back to a different URL form") from e
        url = detail.get("postingUrl") or detail.get("applyUrl")
        if not url:
            raise ScrapeShapeError(
                f"SmartRecruiters posting {posting_id} on '{slug}' has no postingUrl "
                f"or applyUrl in its detail reply")
        description = _description(detail)

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

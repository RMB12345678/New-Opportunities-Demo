"""Pulls open job postings from an Oracle Recruiting Cloud (Oracle Cloud HCM)
candidate-experience site.

A board a human sees at

    https://{host}/hcmUI/CandidateExperience/{lang}/sites/{siteNumber}

is backed by a public JSON endpoint (confirmed against live Cooper and ICU
Medical sites, not assumed):

    GET https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions
        ?onlyData=true&expand=requisitionList.secondaryLocations
        &finder=findReqs;siteNumber={siteNumber},limit=25,offset={n},keyword={term},sortBy=POSTING_DATES_DESC

The reply is {"items": [{"TotalJobsCount": n, "requisitionList": [...]}]}.
Each requisition carries Id, Title, PostedDate and PrimaryLocation. The job's
public page is {site base}/job/{Id}.

The siteNumber (CX_1, CX_1001, ...) is the last segment of the saved Careers
URL itself, so no network lookup is needed to find it. A URL that does not
have that shape is raised on, not guessed at: the host alone does not say
which of a tenant's sites is the company's.

Known failure modes (all raise, none return []):
  * the saved URL is not a candidate-experience URL;
  * the endpoint answers with something that is not a requisition list
    (login wall, maintenance page, changed API).
"""
import re
from urllib.parse import quote

from http_client import session
from scrapers.common import (
    SENIOR_SEARCH_TERMS, USER_AGENT, ScrapeShapeError, dedupe_by_url, polite_sleep,
)

PAGE_SIZE = 25
MAX_PAGES = 200  # 5,000 postings per term; stops a malformed total looping forever

_SITE_URL = re.compile(
    r"^https?://(?P<host>[a-z0-9.\-]+\.oraclecloud\.com)"
    r"/hcmUI/CandidateExperience/(?P<lang>[A-Za-z\-]+)/sites/(?P<site>[A-Za-z0-9_\-]+)",
    re.IGNORECASE,
)


def parse_site_url(careers_url):
    m = _SITE_URL.match((careers_url or "").strip())
    if not m:
        return None
    return m.group("host").lower(), m.group("lang"), m.group("site")


def _get(host, site, term, offset):
    # The finder is one parameter whose parts are separated by commas, so the
    # keyword is percent-encoded by hand: a raw space or comma in it would be
    # read as part of the finder syntax.
    finder = (f"findReqs;siteNumber={site},limit={PAGE_SIZE},offset={offset},"
              f"keyword={quote(term, safe='')},sortBy=POSTING_DATES_DESC")
    resp = session.get(
        f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions",
        # expand is required: without it the reply has no requisitionList at
        # all (confirmed live — the strict shape check below is what caught
        # it), and onlyData alone is not enough.
        params={"onlyData": "true", "expand": "requisitionList.secondaryLocations",
                "finder": finder},
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError:
        raise ScrapeShapeError(f"{host}: Oracle answered 200 but not with JSON "
                               f"(content-type {resp.headers.get('content-type')})")


def _requisitions(company_name, data):
    """items[0].requisitionList and the total, or raise: an answer without
    them is "couldn't parse", never an empty board."""
    items = data.get("items") if isinstance(data, dict) else None
    if (not items or not isinstance(items[0], dict)
            or not isinstance(items[0].get("requisitionList"), list)):
        raise ScrapeShapeError(
            f"{company_name}: Oracle Recruiting Cloud answered 200 but without "
            f"items[0].requisitionList — refusing to read that as an empty board")
    return items[0]["requisitionList"], items[0].get("TotalJobsCount") or 0


def fetch_jobs(company_name, careers_url, search_terms=None):
    parsed = parse_site_url(careers_url)
    if not parsed:
        raise ScrapeShapeError(
            f"{company_name}: the saved Careers URL ({careers_url}) is not an Oracle "
            f"candidate-experience URL (…/hcmUI/CandidateExperience/<lang>/sites/<site>), "
            f"so the site number can't be read from it")
    host, lang, site = parsed
    base = f"https://{host}/hcmUI/CandidateExperience/{lang}/sites/{site}"

    jobs = []
    for term in (search_terms or SENIOR_SEARCH_TERMS):
        offset = 0
        for _ in range(MAX_PAGES):
            reqs, total = _requisitions(company_name, _get(host, site, term, offset))
            for r in reqs:
                jobs.append({
                    "company": company_name,
                    "title": r.get("Title"),
                    "location": r.get("PrimaryLocation"),
                    "url": f"{base}/job/{r['Id']}" if r.get("Id") else None,
                    "posted_at": r.get("PostedDate"),
                    "description": None,
                    "source_ats": "Oracle Recruiting Cloud",
                })
            offset += PAGE_SIZE
            polite_sleep()
            if not reqs or offset >= total:
                break
    return dedupe_by_url(jobs)

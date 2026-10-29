"""Pulls open job postings from a Workday (myworkdayjobs.com) job board.

Workday renders its job list from a background JSON call, not from static
HTML, so these companies used to fall through to scrapers/html_generic.py
and come back with zero jobs. That was the single biggest source of false
zeros in the pipeline: every Workday-tagged company reported 0 open roles
while actually having hundreds.

The board that a human sees at

    https://{host}.wd{n}.myworkdayjobs.com/{site}

is backed by an undocumented but stable JSON endpoint:

    POST https://{host}.wd{n}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
    {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}

Everything below was confirmed against live tenants rather than assumed;
the surprises are recorded inline because each one cost a debugging pass.
"""
import re
from urllib.parse import urlparse, parse_qs

from http_client import session
from scrapers.common import ScrapeShapeError, dedupe_by_url, polite_sleep

# Workday rejects the bare programmatic request on some tenants, so send
# what a browser sends. This is not evasion of a bot check — the boards are
# public and unauthenticated — it is just that the endpoint is picky about
# Accept in particular, and answers normally once it is present.
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
}

# Workday caps page size at 20. Asking for 50 or 100 does not return a
# bigger page, it returns HTTP 400 — so this is a hard protocol limit, not
# a politeness setting, and raising it will break every board at once.
PAGE_SIZE = 20

# Stops a malformed `total` or a board that never returns an empty page
# from looping forever. 500 pages * 20 = 10,000 postings, comfortably more
# than the largest board here (Abbott, ~2,000).
MAX_PAGES = 500

# A Workday internal id: 32 hex characters. Used to tell a facet filter in
# a saved Careers URL (?hiringCompany=75705bdd...) apart from ordinary
# query junk like ?utm_source=linkedin or ?q=engineer.
_WORKDAY_ID = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)

# Matches the public board URL. Two details this has to get right:
#   - `wd\d+` not `wd\d`: Acumed's real board is on wd501, and a
#     single-digit pattern silently truncated it to wd5, which is a
#     DIFFERENT (dead) host that answered every request with a 500. That
#     was the "too many 500 error responses" failure.
#   - the optional locale segment: boards are linked both as
#     /{site} and as /en-US/{site}, and treating "en-US" as the site name
#     yields a 404.
_BOARD_URL = re.compile(
    r"^https?://(?P<host>[a-z0-9\-]+)\.(?P<wd>wd\d+)\.myworkdayjobs\.com"
    r"(?:/[a-z]{2}-[A-Z]{2})?"
    r"/(?P<site>[^/?#]+)",
    re.IGNORECASE,
)


def parse_board_url(careers_url):
    """Turn a saved Careers URL into the parameters fetch_jobs() needs.

    Returns a dict of {host, wd, tenant, site, applied_facets} or None if
    the URL is not a myworkdayjobs.com board at all. Returning None
    matters: the caller must then flag the company for manual review
    rather than guessing a tenant, because a guessed tenant either 404s or
    — worse — resolves to some other company's board.

    The tenant in the API path is usually the host subdomain, but not
    always literally: United Therapeutics is hosted at `vhr-unither` while
    its tenant is `vhr_unither`. Hyphen-to-underscore is the observed rule,
    and the wrong form returns 422 for every site name you try, which is
    what makes this failure look like a bad site rather than a bad tenant.
    """
    if not careers_url:
        return None

    m = _BOARD_URL.match(careers_url.strip())
    if not m:
        return None

    host = m.group("host").lower()
    site = m.group("site")

    # Any query param whose value looks like a Workday id is a facet
    # filter — this is how shared tenants expose a single subsidiary (see
    # fetch_jobs). The key is NOT always "hiringCompany": Halma uses that,
    # but Marmon uses a custom field named
    # "CF-EE-External_Company_Customer_Org_Job_Posting_Anchor_Extended".
    # So the key is carried through from the URL verbatim rather than
    # hardcoded, and validated against the board before it is trusted.
    applied_facets = {}
    for key, values in parse_qs(urlparse(careers_url).query).items():
        ids = [v for v in values if _WORKDAY_ID.match(v)]
        if ids:
            applied_facets[key] = ids

    return {
        "host": host,
        "wd": m.group("wd").lower(),
        "tenant": host.replace("-", "_"),
        "site": site,
        "applied_facets": applied_facets,
    }


def _post(api_url, payload):
    resp = session.post(api_url, json=payload, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _expect_shape(company_name, data):
    """A board answers with {"total": n, "jobPostings": [...]}. Anything
    else — an HTML login wall, a maintenance page that still says 200, a
    changed API — is "couldn't parse", not "zero jobs": raising lands the
    company in Scrape Status with a reason, where returning [] would score
    it as an empty board and close its rows."""
    if not isinstance(data, dict) or ("jobPostings" not in data and "total" not in data):
        raise ScrapeShapeError(
            f"{company_name}: Workday answered 200 but not with a job list "
            f"(got {type(data).__name__}{': ' + str(sorted(data))[:80] if isinstance(data, dict) else ''}) "
            f"— refusing to read that as an empty board")
    return data


def _collect_term(company_name, api_url, applied_facets, term):
    """Every posting matching one search term, paged to exhaustion."""
    postings, offset, total = [], 0, None
    for _ in range(MAX_PAGES):
        data = _expect_shape(company_name, _post(api_url, {
            "appliedFacets": applied_facets,
            "limit": PAGE_SIZE,
            "offset": offset,
            "searchText": term,
        }))
        if total is None:
            total = data.get("total") or 0
        batch = data.get("jobPostings") or []
        postings.extend(batch)
        offset += PAGE_SIZE
        if not batch or offset >= total:
            break
        polite_sleep()
    return postings


def fetch_jobs(company_name, host=None, wd=None, tenant=None, site=None,
               applied_facets=None, search_terms=None):
    """Fetch every posting on a Workday board, paging until the board is
    exhausted.

    Unlike the Greenhouse/Lever/Ashby scrapers, there is no name-guessing
    fallback here. Those platforms key off a single slug that is usually
    derivable from the company name; a Workday board needs a host, a
    tenant, a site and sometimes a facet id, and a wrong guess does not
    reliably 404 — on a shared tenant it can return a full board of some
    other company's jobs. So the caller must supply real parameters parsed
    from a confirmed URL, and a company we cannot parse is flagged for a
    human instead (invariant: an error is not an answer).

    Shared tenants: some Workday tenants host many portfolio companies on
    one board, filtered by a facet. Microsurgical Technology's postings
    live on Halma's board; Acumed's live on Marmon's. When the saved URL
    carries that filter it is passed through as `appliedFacets`, and the
    facet key is verified against the board's own advertised facet list
    before use. If the filter does not check out this raises rather than
    quietly returning the whole unfiltered tenant — a mixed job list
    attributed to one subsidiary is worse than an honest failure, because
    nothing downstream would ever catch it.

    search_terms: when given, run one narrow query per term instead of
    paging the whole board, and dedupe by URL. Used for the Large MedTech
    tier, where only senior titles are kept and a board like Abbott's runs
    to ~2,000 postings. None (the default) keeps the full-board behaviour
    every other company relies on.
    """
    if not (host and wd and site):
        raise ValueError(
            f"{company_name}: Workday needs host, wd and site parsed from a real "
            f"board URL; got host={host!r} wd={wd!r} site={site!r}"
        )

    tenant = tenant or host.replace("-", "_")
    applied_facets = applied_facets or {}
    api_url = f"https://{host}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"

    first = _expect_shape(company_name, _post(api_url, {
        "appliedFacets": {},
        "limit": PAGE_SIZE,
        "offset": 0,
        "searchText": "",
    }))

    if applied_facets:
        _verify_facets(company_name, first, applied_facets)
        first = _expect_shape(company_name, _post(api_url, {
            "appliedFacets": applied_facets,
            "limit": PAGE_SIZE,
            "offset": 0,
            "searchText": "",
        }))

    if search_terms:
        postings = []
        for term in search_terms:
            postings.extend(_collect_term(company_name, api_url, applied_facets, term))
            polite_sleep()
    else:
        total = first.get("total") or 0
        postings = list(first.get("jobPostings") or [])

        # Page until we have everything `total` promised. The empty-page check
        # is the real terminator: `total` is trustworthy in practice but a
        # board that shrinks mid-scrape would otherwise spin.
        for page in range(1, MAX_PAGES):
            if len(postings) >= total:
                break
            batch = _expect_shape(company_name, _post(api_url, {
                "appliedFacets": applied_facets,
                "limit": PAGE_SIZE,
                "offset": page * PAGE_SIZE,
                "searchText": "",
            })).get("jobPostings") or []
            if not batch:
                break
            postings.extend(batch)

    base = f"https://{host}.{wd}.myworkdayjobs.com/en-US/{site}"
    jobs = [
        {
            "company": company_name,
            "title": job.get("title"),
            "location": job.get("locationsText"),
            "url": base + job["externalPath"] if job.get("externalPath") else None,
            # Workday reports recency as prose ("Posted 4 Days Ago",
            # "Posted 30+ Days Ago"), not a timestamp. Nothing downstream
            # parses posted_at today, so it is passed through as-is rather
            # than invented into a date that would be wrong by up to a
            # month for the "30+" bucket.
            "posted_at": job.get("postedOn"),
            # The list endpoint carries no description. There is a
            # per-posting detail endpoint, but using it would mean one
            # extra HTTP round trip per job — ~2,000 of them for Abbott
            # alone — for a field the scorer already treats as optional
            # ("not available"). Not worth the run time or the load.
            "description": None,
            "source_ats": "Workday",
        }
        for job in postings
    ]
    return dedupe_by_url(jobs) if search_terms else jobs


def _verify_facets(company_name, response, applied_facets):
    """Check a facet filter against what the board actually advertises.

    Workday does reject an unknown facet key or a bogus id with a 400, so
    a bad filter cannot silently return unfiltered results. This check
    exists anyway because it turns that opaque 400 into a message naming
    the bad key and listing the valid ones, which is what ends up in the
    Scrape Note where a human will read it.
    """
    known = {
        f.get("facetParameter"): f
        for f in (response.get("facets") or [])
        if f.get("facetParameter")
    }
    for key, ids in applied_facets.items():
        facet = known.get(key)
        if facet is None:
            raise ValueError(
                f"{company_name}: the saved Careers URL filters on '{key}', but this "
                f"board advertises no such facet (it has: {sorted(known) or 'none'}). "
                f"Refusing to fall back to the unfiltered board, which would mix in "
                f"other companies' jobs."
            )
        valid = {v.get("id") for v in (facet.get("values") or [])}
        unknown = [i for i in ids if i not in valid]
        if unknown:
            raise ValueError(
                f"{company_name}: the saved Careers URL filters '{key}' on "
                f"{unknown}, which this board does not list as a valid value. "
                f"The subsidiary may have been renamed or removed; refusing to "
                f"fall back to the unfiltered board."
            )

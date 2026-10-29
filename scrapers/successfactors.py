"""Pulls open job postings from a branded SAP SuccessFactors career site
(the "RMK" Career Site Builder template).

There is no public JSON API. These sites render a server-side search page:

    GET https://{careers-domain}/search/?q={term}&startrow={n}

Rows are <tr class="data-row"> with the title in a.jobTitle-link, the
location in span.jobLocation, the date in span.jobDate, 25 rows per page,
paged by startrow. A "Results 1 – 25 of 35" label (.paginationLabel) carries
the total. Confirmed against live Bausch + Lomb, Coloplast and Sonova.

This is the most fragile scraper in the pipeline, because the likely failure
is "page loaded, my selectors found nothing", and that looks exactly like an
empty board. So a page is only believed in two cases:

  * it has at least one data-row AND a pagination total, or
  * it has the server-rendered heading <h1>Search results for "…"</h1> and no
    rows — a genuinely empty result (Coloplast answers a nonsense query with
    exactly this).

Anything else raises ScrapeShapeError. In practice that is:
  * B. Braun: an RMK site whose result list is filled in by JavaScript. The
    page is a 3 KB cookie-banner shell with no results heading.
  * Steris and Straumann: not RMK sites at all (no /search/ page of this shape).
  * career{N}.successfactors.com/career?company=…: the other common shape,
    also JS-rendered. Raised on up front, without a request — plain requests
    cannot read it, and the honest answer is "needs a browser".
"""
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from http_client import session
from scrapers.common import (
    SENIOR_SEARCH_TERMS, USER_AGENT, ScrapeShapeError, dedupe_by_url, polite_sleep,
)

PAGE_SIZE = 25
MAX_PAGES = 100

# career5.successfactors.com, career.successfactors.eu, performancemanager5.successfactors.com
_JS_HOST = re.compile(r"^(?:career|performancemanager)\d*\.(?:successfactors|sapsf)\.(?:com|eu)$", re.I)
_TOTAL = re.compile(r"of\s+([\d,]+)\s*$")


def _parse_page(company_name, url, html):
    """Return (rows, total). Raises unless the page is recognisably an RMK
    result page — see the module docstring for the two accepted shapes."""
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("tr.data-row")
    label = soup.select_one(".paginationLabel")
    heading = soup.select_one("h1")
    has_results_heading = bool(heading and re.search(
        r"search results for", heading.get_text(" ", strip=True), re.I))

    if rows:
        m = _TOTAL.search(label.get_text(" ", strip=True)) if label else None
        if not m:
            raise ScrapeShapeError(
                f"{company_name}: {url} has job rows but no 'Results x – y of N' "
                f"total, so paging can't be trusted — markup has probably changed")
        return rows, int(m.group(1).replace(",", ""))
    if has_results_heading:
        return [], 0
    raise ScrapeShapeError(
        f"{company_name}: {url} is not a server-rendered SuccessFactors result page "
        f"(no job rows and no 'Search results for' heading). Likely JS-rendered or "
        f"not an RMK site — needs a browser or a different scraper, not read as 0 jobs")


def _row_to_job(company_name, base, row):
    link = row.select_one("a.jobTitle-link")
    if not link or not link.get("href"):
        raise ScrapeShapeError(
            f"{company_name}: a data-row had no a.jobTitle-link — markup has changed")
    loc = row.select_one("span.jobLocation")
    date = row.select_one("span.jobDate")
    return {
        "company": company_name,
        "title": link.get_text(" ", strip=True),
        "location": loc.get_text(" ", strip=True) if loc else None,
        "url": urljoin(base, link["href"]),
        "posted_at": date.get_text(" ", strip=True) if date else None,
        "description": None,
        "source_ats": "SuccessFactors",
    }


def fetch_jobs(company_name, careers_url, search_terms=None):
    parsed = urlparse((careers_url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        raise ScrapeShapeError(f"{company_name}: no usable Careers URL ({careers_url!r})")
    if _JS_HOST.match(parsed.netloc):
        raise ScrapeShapeError(
            f"{company_name}: {parsed.netloc} is a JS-rendered SuccessFactors career page "
            f"(career?company=…) — needs a browser, plain requests can't read it")
    base = f"{parsed.scheme}://{parsed.netloc}"

    jobs = []
    for term in (search_terms or SENIOR_SEARCH_TERMS):
        startrow = 0
        for _ in range(MAX_PAGES):
            url = f"{base}/search/"
            resp = session.get(url, params={"q": term, "startrow": startrow},
                               headers={"User-Agent": USER_AGENT}, timeout=30)
            resp.raise_for_status()
            rows, total = _parse_page(company_name, resp.url, resp.text)
            jobs.extend(_row_to_job(company_name, base, r) for r in rows)
            startrow += PAGE_SIZE
            polite_sleep()
            if not rows or startrow >= total:
                break
    return dedupe_by_url(jobs)

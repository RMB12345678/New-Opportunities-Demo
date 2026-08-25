"""
Best-effort scraper for company career pages that don't run a known ATS.

There's no universal structure for these pages, so this uses heuristics:
  1. Fetch the page.
  2. Look for repeated elements that resemble job listings — either
     <a> tags whose text looks like a job title, or common container
     patterns (class/id containing "job", "position", "opening", "career",
     "role").
  3. De-duplicate and filter obvious non-jobs (nav links, footer links).

This will NOT work on every site — many in-house pages render their job
list via JavaScript after the page loads, which a plain HTTP fetch can't
see. Those need a per-company override (see OVERRIDES below) or a
headless-browser tool, which isn't included here. Anything this scraper
can't confidently parse is left for output/skipped_companies.json rather
than guessing and returning junk.
"""
import re
from bs4 import BeautifulSoup

from http_client import session

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MedTechTargetScraper/1.0)"}

# Words that make an <a> tag's text look like a real job posting.
JOB_TITLE_HINTS = re.compile(
    r"\b(engineer|manager|director|specialist|coordinator|analyst|"
    r"scientist|associate|lead|head of|vp|svp|chief|president|nurse|"
    r"technician|representative|executive|officer|administrator|"
    r"designer|developer|architect|counsel|recruiter|intern)\b",
    re.IGNORECASE,
)

# Words that mean an <a> tag is almost certainly NOT a job posting, even if
# it happens to contain a hint word above (e.g. "Careers" nav link).
NAV_NOISE = re.compile(
    r"^(home|about|contact|careers|jobs|blog|news|privacy|terms|login|"
    r"sign in|apply now|view all|see all|learn more)$",
    re.IGNORECASE,
)

# Per-company overrides for sites the generic heuristics can't handle
# (e.g. JavaScript-rendered listings, unusual page structure). Add entries
# here as you find companies the generic scraper misses. Each value is a
# CSS selector (passed to BeautifulSoup's .select()) for the elements that
# contain each job's title text.
OVERRIDES = {
    # "Company Name": "css.selector.for.job.title.elements",
}


def fetch_jobs(company_name, careers_url):
    if not careers_url:
        raise ValueError(f"No careers_url provided for {company_name}")

    resp = session.get(careers_url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    if company_name in OVERRIDES:
        elements = soup.select(OVERRIDES[company_name])
        titles = [el.get_text(strip=True) for el in elements]
        links = [el.get("href") if el.name == "a" else None for el in elements]
    else:
        titles, links = _generic_extract(soup, careers_url)

    jobs = []
    seen = set()
    for title, link in zip(titles, links):
        title = title.strip()
        if not title or title.lower() in seen:
            continue
        seen.add(title.lower())
        jobs.append({
            "company": company_name,
            "title": title,
            "location": None,  # generic scraper doesn't reliably find this
            "url": link if link else careers_url,
            "posted_at": None,
            "description": None,  # generic scraper has no reliable way to find posting text
            "source_ats": "HTML (generic scrape)",
        })

    return jobs


def _generic_extract(soup, base_url):
    titles, links = [], []

    for a in soup.find_all("a"):
        text = a.get_text(strip=True)
        if not text or len(text) > 100:
            continue
        if NAV_NOISE.match(text):
            continue
        if not JOB_TITLE_HINTS.search(text):
            continue

        href = a.get("href")
        if href and href.startswith("/"):
            from urllib.parse import urljoin
            href = urljoin(base_url, href)

        titles.append(text)
        links.append(href)

    return titles, links

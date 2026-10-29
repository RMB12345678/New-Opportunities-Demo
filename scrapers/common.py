"""Shared pieces for the scrapers that read a board off a saved Careers URL
(Oracle Recruiting Cloud, SuccessFactors, iCIMS, Avature, Taleo) and for the
Large MedTech tier's search-term loop.

Greenhouse, Lever and Ashby need one slug. These platforms need a host plus a
site or tenant, so they take `fetch_jobs(company_name, careers_url)` instead.

Two rules every scraper using this module follows, both learned from the
pipeline's history rather than assumed:

  * **Zero jobs and "couldn't parse" are different answers.** A board that
    genuinely has nothing open returns []. A response that is not the shape
    the scraper expects — a JS-rendered shell, changed markup, a login wall —
    must RAISE (ScrapeShapeError), so the company lands in Scrape Status with
    a real reason instead of silently scoring as "0 jobs, OK". A scraper that
    returns [] for something it could not read is the false-zero bug that
    Workday-on-HTML produced for every Workday company.
  * **Search terms, not paging through everything.** The tier these scrapers
    serve keeps only senior titles, and a board of several thousand postings
    is slow and rude to read in full. Each term is one narrow query.
"""
import time

# One query per term. Matches the intent of run.SENIOR_TITLE_RE; the regex
# still runs afterwards and is what actually decides what is kept, so a term
# that over-fetches is harmless and a term missing here only costs recall.
SENIOR_SEARCH_TERMS = ["vice president", "director", "head of", "general manager", "chief"]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Seconds between requests to one host.
POLITE_DELAY = 0.5


class ScrapeShapeError(RuntimeError):
    """The response was not the shape this scraper understands. Raised
    instead of returning [] — see the module docstring."""


def polite_sleep():
    time.sleep(POLITE_DELAY)


def dedupe_by_url(jobs):
    """First occurrence of each URL wins. Several search terms return the
    same posting (a "Director, X" matches both "director" and, say, "chief
    of staff"), and a URL is a posting's identity downstream (invariant 20)."""
    seen, out = set(), []
    for job in jobs:
        url = job.get("url")
        if url and url in seen:
            continue
        if url:
            seen.add(url)
        out.append(job)
    return out

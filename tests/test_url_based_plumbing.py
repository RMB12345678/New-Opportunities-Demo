"""Tests for the shared plumbing behind the URL-based scrapers and for the
Workday search-term mode. No network.

  - aliases resolve to the canonical platform, and "Oracle Taleo" never
    resolves to Oracle Recruiting Cloud (different product, different API);
  - a URL-based scraper gets the saved Careers URL, has no guessed fallback,
    and a missing URL or a raise lands in Scrape Status instead of OK;
  - Workday's search-term mode queries once per term, dedupes by URL, and an
    answer that is not a job list raises instead of reading as zero jobs.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run
from scrapers import workday
from scrapers.common import ScrapeShapeError, dedupe_by_url
from notion.client import SCRAPE_OK, SCRAPE_FAILED, SCRAPE_NO_URL


def _fake(name, careers_url):
    return [{"company": name, "title": "VP, X", "url": "https://x/1", "location": None,
             "posted_at": None, "source_ats": "Fake"}]


@pytest.fixture
def fake_platforms(monkeypatch):
    monkeypatch.setitem(run.SCRAPERS, "Oracle Recruiting Cloud", _fake)
    monkeypatch.setitem(run.SCRAPERS, "SuccessFactors", _fake)


def test_aliases_resolve_to_canonical_name(fake_platforms):
    for text in ["Oracle Recruiting Cloud", "Oracle Cloud HCM", "Oracle Recruiting",
                 "oracle recruiting cloud (hcm)"]:
        assert run.resolve_scraper(text)[1] == "Oracle Recruiting Cloud", text
    assert run.resolve_scraper("SAP SuccessFactors (Career Site Builder)")[1] == "SuccessFactors"


def test_taleo_is_not_oracle_recruiting_cloud(fake_platforms):
    # No Taleo scraper exists, so Taleo text must resolve to nothing — in
    # particular not to Oracle Recruiting Cloud.
    assert run.resolve_scraper("Oracle Taleo (careers site uses /go/ ...)")[1] is None
    assert run.resolve_scraper("Taleo")[1] is None


def _row(**kw):
    base = {"company": "Big Co", "tier": None, "sector": "s", "scrape_method": "API",
            "ats_platform": "Oracle Recruiting Cloud", "careers_url": "https://h/x"}
    base.update(kw)
    return base


def test_url_based_scraper_gets_the_saved_url(fake_platforms):
    jobs, ok, statuses = run.run_scrapers([_row()])
    assert len(jobs) == 1 and "Big Co" in ok and statuses[0]["status"] == SCRAPE_OK


def test_url_based_without_url_is_no_careers_url_not_a_guess(fake_platforms):
    jobs, ok, statuses = run.run_scrapers([_row(careers_url=None)])
    assert jobs == [] and "Big Co" not in ok and statuses[0]["status"] == SCRAPE_NO_URL


def test_url_based_raise_is_failed_and_not_scraped_ok(monkeypatch):
    def boom(name, url):
        raise ScrapeShapeError("JS-rendered shell")
    monkeypatch.setitem(run.SCRAPERS, "SuccessFactors", boom)
    jobs, ok, statuses = run.run_scrapers([_row(ats_platform="SuccessFactors")])
    assert "Big Co" not in ok
    assert statuses[0]["status"] == SCRAPE_FAILED and "JS-rendered" in statuses[0]["note"]


def test_dedupe_by_url_keeps_first():
    jobs = [{"url": "a", "title": 1}, {"url": "a", "title": 2}, {"url": None}, {"url": None}]
    assert [j.get("title") for j in dedupe_by_url(jobs)] == [1, None, None]


# --- Workday ---------------------------------------------------------------

def _posting(n):
    return {"title": f"Director {n}", "externalPath": f"/job/{n}", "locationsText": "X",
            "postedOn": "Posted Today"}


def test_workday_search_terms_query_each_term_and_dedupe(monkeypatch):
    monkeypatch.setattr(workday, "polite_sleep", lambda: None)
    seen_terms = []

    def fake_post(url, payload):
        term = payload["searchText"]
        seen_terms.append(term)
        if term == "":
            return {"total": 5000, "jobPostings": [_posting(0)]}
        # every term returns posting 1 (shared) and one of its own
        return {"total": 2, "jobPostings": [_posting(1), _posting(len(seen_terms))]}

    monkeypatch.setattr(workday, "_post", fake_post)
    jobs = workday.fetch_jobs("Big Co", host="big", wd="wd1", site="S",
                              search_terms=["director", "chief"])
    assert seen_terms.count("director") >= 1 and seen_terms.count("chief") >= 1
    urls = [j["url"] for j in jobs]
    assert len(urls) == len(set(urls))
    assert any(u.endswith("/job/1") for u in urls)


def test_workday_non_joblist_answer_raises_not_zero(monkeypatch):
    monkeypatch.setattr(workday, "_post", lambda url, payload: {"errorCode": "maintenance"})
    with pytest.raises(ScrapeShapeError):
        workday.fetch_jobs("Big Co", host="big", wd="wd1", site="S")


def test_workday_genuinely_empty_board_is_empty_list(monkeypatch):
    monkeypatch.setattr(workday, "_post", lambda url, payload: {"total": 0, "jobPostings": []})
    assert workday.fetch_jobs("Big Co", host="big", wd="wd1", site="S") == []

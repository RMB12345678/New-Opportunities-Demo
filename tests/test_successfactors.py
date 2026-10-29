"""Tests for the SuccessFactors (RMK) scraper. Pages are fixtures built from
the markup seen live on Coloplast / Bausch + Lomb / Sonova; HTTP is stubbed.

The rule under test: "page loaded but my selectors found nothing" must raise,
and only a recognisable empty result page may return [].
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scrapers import successfactors as sf
from scrapers.common import ScrapeShapeError

ROW = """<tr class="data-row"><td class="colTitle">
<span class="jobTitle hidden-phone"><a class="jobTitle-link" href="{href}">{title}</a></span>
<span class="jobTitle visible-phone"><a class="jobTitle-link" href="{href}">{title}</a></span>
<span class="jobLocation">Kansas City, MO, US</span><span class="jobDate">29 Sept 2026</span>
</td></tr>"""


def page(rows=(), label="Results 1 – 1 of 1", h1='Search results for "director".'):
    body = "".join(ROW.format(href=h, title=t) for h, t in rows)
    lab = f'<span class="paginationLabel">{label}</span>' if label else ""
    return f'<html><h1>{h1}</h1>{lab}<table id="searchresults">{body}</table></html>'


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(sf, "polite_sleep", lambda: None)


class Resp:
    def __init__(self, text, url="https://careers.x.com/search/"):
        self.text, self.url = text, url

    def raise_for_status(self):
        pass


def _serve(monkeypatch, html_for):
    monkeypatch.setattr(sf.session, "get", lambda url, params=None, **kw: Resp(html_for(params)))


def test_parses_rows_into_job_dicts(monkeypatch):
    html = page([("/Co/job/Dir-Sales/123/", "Director, Sales")])
    _serve(monkeypatch, lambda p: html)
    jobs = sf.fetch_jobs("Co", "https://careers.x.com/go/USA/1/", search_terms=["director"])
    assert jobs == [{
        "company": "Co", "title": "Director, Sales", "location": "Kansas City, MO, US",
        "url": "https://careers.x.com/Co/job/Dir-Sales/123/", "posted_at": "29 Sept 2026",
        "description": None, "source_ats": "SuccessFactors"}]


def test_pages_by_startrow_until_total(monkeypatch):
    seen = []

    def html_for(p):
        seen.append(p["startrow"])
        n = 25 if p["startrow"] == 0 else 5
        start = p["startrow"]
        return page([(f"/j/{start + i}/", f"Director {start + i}") for i in range(n)],
                    label="Results 1 – 25 of 30")

    _serve(monkeypatch, html_for)
    jobs = sf.fetch_jobs("Co", "https://careers.x.com/", search_terms=["director"])
    assert seen == [0, 25] and len(jobs) == 30


def test_recognised_empty_result_page_is_empty_list(monkeypatch):
    _serve(monkeypatch, lambda p: page(rows=(), label=None))
    assert sf.fetch_jobs("Co", "https://careers.x.com/", search_terms=["director"]) == []


def test_js_shell_raises_not_empty(monkeypatch):
    # B. Braun: a cookie banner, a search-wrapper div, no results heading.
    shell = '<html><title>Director</title><div id="search-wrapper"></div>cookies</html>'
    _serve(monkeypatch, lambda p: shell)
    with pytest.raises(ScrapeShapeError):
        sf.fetch_jobs("Co", "https://jobs.x.com/", search_terms=["director"])


def test_rows_without_a_total_raise(monkeypatch):
    _serve(monkeypatch, lambda p: page([("/j/1/", "Director")], label=None))
    with pytest.raises(ScrapeShapeError):
        sf.fetch_jobs("Co", "https://careers.x.com/", search_terms=["director"])


def test_career_n_successfactors_com_raises_without_a_request(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not fetch a JS-rendered career page")
    monkeypatch.setattr(sf.session, "get", boom)
    with pytest.raises(ScrapeShapeError, match="needs a browser"):
        sf.fetch_jobs("Co", "https://career5.successfactors.com/career?company=acme")

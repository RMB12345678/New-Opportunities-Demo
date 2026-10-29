"""Tests for the Oracle Recruiting Cloud scraper. HTTP is stubbed.

The property that matters is the parse-vs-empty rule: a genuinely empty board
is [], while an answer that is not a requisition list raises.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scrapers import oracle_rc
from scrapers.common import ScrapeShapeError

URL = "https://hcjy.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(oracle_rc, "polite_sleep", lambda: None)


def _reply(reqs, total=None):
    return {"items": [{"TotalJobsCount": len(reqs) if total is None else total,
                       "requisitionList": reqs}]}


def _req(i, title="Director X"):
    return {"Id": str(i), "Title": title, "PostedDate": "2026-10-01",
            "PrimaryLocation": "Somewhere"}


def test_parses_host_lang_and_site():
    assert oracle_rc.parse_site_url(URL) == ("hcjy.fa.us2.oraclecloud.com", "en", "CX_1")
    assert oracle_rc.parse_site_url("https://careers.cooperco.com/") is None


def test_maps_fields_and_builds_job_url(monkeypatch):
    monkeypatch.setattr(oracle_rc, "_get", lambda h, s, t, o: _reply([_req(7)]))
    jobs = oracle_rc.fetch_jobs("Cooper", URL, search_terms=["director"])
    assert jobs == [{
        "company": "Cooper", "title": "Director X", "location": "Somewhere",
        "url": URL + "/job/7", "posted_at": "2026-10-01", "description": None,
        "source_ats": "Oracle Recruiting Cloud"}]


def test_pages_until_total_and_dedupes_across_terms(monkeypatch):
    calls = []

    def fake(h, s, term, offset):
        calls.append((term, offset))
        if offset == 0:
            return _reply([_req(i) for i in range(25)], total=30)
        return _reply([_req(i) for i in range(25, 30)], total=30)

    monkeypatch.setattr(oracle_rc, "_get", fake)
    jobs = oracle_rc.fetch_jobs("Cooper", URL, search_terms=["director", "chief"])
    assert len(jobs) == 30  # the second term returns the same 30 URLs
    assert ("director", 25) in calls and ("chief", 25) in calls


def test_empty_board_is_an_empty_list(monkeypatch):
    monkeypatch.setattr(oracle_rc, "_get", lambda h, s, t, o: _reply([], total=0))
    assert oracle_rc.fetch_jobs("Cooper", URL, search_terms=["director"]) == []


@pytest.mark.parametrize("bad", [{}, {"items": []}, {"items": [{"x": 1}]}, [], "<html>"])
def test_unexpected_shape_raises_not_empty(monkeypatch, bad):
    monkeypatch.setattr(oracle_rc, "_get", lambda h, s, t, o: bad)
    with pytest.raises(ScrapeShapeError):
        oracle_rc.fetch_jobs("Cooper", URL, search_terms=["director"])


def test_unparseable_careers_url_raises():
    with pytest.raises(ScrapeShapeError):
        oracle_rc.fetch_jobs("Cooper", "https://careers.cooperco.com/")

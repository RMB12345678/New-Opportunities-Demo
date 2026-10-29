"""Tests for the Large MedTech tier: the senior-title filter and the scorer's
tier line.

What is worth protecting:
  - the filter applies to the tier and ONLY the tier — an untiered company
    keeps every posting, including the junior ones;
  - it runs inside run_scrapers(), before anything is returned, so filtered
    roles can never reach scoring or the Notion sync;
  - the tier line appears in the per-job message for tiered jobs and is
    absent (byte-identical message) for everything else, and the cache key
    is still the job URL.

No network, no spend.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run
from scoring import score_jobs
from notion.client import _select_name


def _job(title):
    return {"company": "Big Co", "title": title, "url": f"https://x/{title}"}


def test_regex_matches_senior_titles():
    for t in ["VP, Marketing", "SVP Operations", "Vice President of Sales",
              "Senior Director, Regulatory", "Sr. Director Finance",
              "Associate Director, Quality", "Head of Product",
              "General Manager, North America", "Chief Commercial Officer"]:
        assert run.SENIOR_TITLE_RE.search(t), t


def test_regex_rejects_junior_and_lookalike_titles():
    for t in ["Software Engineer", "Territory Manager", "Sales Representative",
              "Directory Assistant", "Vpn Administrator", "Manager, Supply Chain"]:
        assert not run.SENIOR_TITLE_RE.search(t), t


def test_prepare_jobs_filters_only_the_large_medtech_tier():
    jobs = [_job("VP, Sales"), _job("Field Technician"), _job("Director, IT")]
    tiered = run.prepare_jobs({"company": "Big Co", "tier": "Large MedTech", "sector": "x"},
                              [dict(j) for j in jobs])
    assert [j["title"] for j in tiered] == ["VP, Sales", "Director, IT"]
    assert all(j["company_tier"] == "Large MedTech" for j in tiered)

    untiered = run.prepare_jobs({"company": "Small Co", "tier": None, "sector": "x"},
                                [dict(j) for j in jobs])
    assert len(untiered) == 3
    assert all("company_tier" not in j for j in untiered)
    assert all(j["company_description"] == "x" for j in untiered)


def test_run_scrapers_filters_before_returning(monkeypatch):
    monkeypatch.setitem(run.SCRAPERS, "Greenhouse",
                        lambda name, slug=None: [_job("VP, Sales"), _job("Nurse")])
    row = {"company": "Big Co", "tier": "Large MedTech", "sector": "s",
           "ats_platform": "Greenhouse", "scrape_method": "API",
           "careers_url": "https://boards.greenhouse.io/bigco"}
    jobs, scraped_ok, statuses = run.run_scrapers([row])
    assert [j["title"] for j in jobs] == ["VP, Sales"]
    assert "Big Co" in scraped_ok


def test_tier_line_present_only_for_tiered_jobs():
    assert score_jobs._tier_line({"company_tier": "Large MedTech"}) == \
        "Company tier: Large MedTech (>$1B revenue)\n"
    assert score_jobs._tier_line({}) == ""


def test_cache_key_is_still_the_url():
    j = {"url": "https://x/1", "company": "a", "title": "b", "company_tier": "Large MedTech"}
    assert score_jobs._job_key(j) == "https://x/1"


def test_missing_or_blank_tier_property_reads_as_none():
    assert _select_name(None) is None
    assert _select_name({"type": "select", "select": None}) is None
    assert _select_name({"type": "select", "select": {"name": "Large MedTech"}}) == "Large MedTech"

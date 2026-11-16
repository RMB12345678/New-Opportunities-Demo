"""Offline tests for the remaining punch-list items that need no re-score:
#5, #8 (usage reporting only), #9 (steps 1-2), #10, #11, #12, #17, #27, #28.

Everything is stubbed: no network, no Notion, no Anthropic.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import http_client
import notion.client as nc
import run
from scrapers import html_generic
from scrapers.common import ScrapeShapeError
from scoring import score_jobs

import test_delta_sync as ds


# --- #28: no session sleeps on a server-supplied Retry-After ---------------

@pytest.mark.parametrize("session", [http_client.session, http_client.create_session,
                                     http_client.third_party_session])
def test_no_session_honours_retry_after(session):
    retry = session.get_adapter("https://example.com").max_retries
    assert retry.respect_retry_after_header is False


def test_retry_after_header_is_ignored_when_sleeping(monkeypatch):
    """Retry.sleep() only obeys the header when respect_retry_after_header is
    set. Record every sleep it attempts: none may be the server's 600 s."""
    import urllib3.util.retry as retry_mod
    slept = []
    monkeypatch.setattr(retry_mod.time, "sleep", lambda s: slept.append(s))
    retry = http_client.session.get_adapter("https://example.com").max_retries

    class Resp:
        headers = {"Retry-After": "600"}

    retry.sleep(Resp())
    assert 600 not in slept and all(s < 60 for s in slept), slept


# --- #27: negative text does not name a platform ---------------------------

SIEMENS = ("In-house careers portal - not a standard ATS like Greenhouse, Lever or "
           "Workday")


def test_negated_platform_names_resolve_to_nothing():
    assert run.resolve_scraper(SIEMENS) == (None, None)
    assert run.resolve_scraper("Custom, such as Ashby-style but not Lever")[1] is None


def test_real_platform_text_still_resolves():
    assert run.resolve_scraper("Greenhouse (acquired by Getinge)")[1] == "Greenhouse"
    assert run.resolve_scraper("  Lever ")[1] == "Lever"
    # a negation in an EARLIER sentence does not poison a later claim
    assert run.resolve_scraper("Not Workday. Uses Greenhouse.")[1] == "Greenhouse"


# --- #10: marker text is not a company description -------------------------

def test_clean_sector_drops_markers_and_keeps_descriptions():
    for marker in ("Not found (checked, no info located - clear this field to retry)",
                   "AMBIGUOUS - see console output from this run for candidates",
                   "N/A", "  not found", "", None, "   "):
        assert run.clean_sector(marker) is None, marker
    assert run.clean_sector("Cardiac monitoring devices") == "Cardiac monitoring devices"


def test_prepare_jobs_uses_the_cleaned_sector():
    jobs = run.prepare_jobs({"page_id": "p", "company": "A", "sector": "Not found (x)"},
                            [{"title": "VP"}])
    assert jobs[0]["company_description"] is None


# --- #11: URL-less jobs are dropped before scoring -------------------------

def test_urlless_jobs_are_dropped_and_counted(capsys):
    jobs = [{"company": "A", "title": "x", "url": "http://a/1"},
            {"company": "B", "title": "y", "url": None},
            {"company": "B", "title": "z"}]
    kept = run.drop_urlless_jobs(jobs)
    assert [j["title"] for j in kept] == ["x"]
    assert "dropped 2 posting(s) with no URL" in capsys.readouterr().out


def test_nothing_is_reported_when_every_job_has_a_url(capsys):
    jobs = [{"company": "A", "title": "x", "url": "http://a/1"}]
    assert run.drop_urlless_jobs(jobs) == jobs
    assert capsys.readouterr().out == ""


# --- #12: selectors are pure filters over a passed list --------------------

def _co(name, **kw):
    base = {"page_id": "p-" + name, "company": name, "sector": None, "hq": None,
            "source": None, "ats_platform": None, "scrape_method": None,
            "careers_url": None, "website": None, "has_icon": False, "tier": None}
    base.update(kw)
    return base


def test_selectors_do_not_rescan_notion_when_given_a_list(monkeypatch):
    def boom():
        raise AssertionError("get_all_companies() must not be called")

    monkeypatch.setattr(nc, "get_all_companies", boom)
    rows = [_co("Acme"), _co("Beta", ats_platform="Greenhouse")]
    assert [r["company"] for r in nc.get_companies_missing_ats(rows)] == ["Acme"]
    assert [r["company"] for r in nc.get_companies_missing_profile(rows)] == ["Beta"]
    assert [r["company"] for r in nc.get_companies_missing_url(rows)] == ["Beta"]
    assert [r["company"] for r in nc.get_companies_missing_website(rows)] == ["Acme", "Beta"]


def test_selectors_still_read_notion_when_given_nothing(monkeypatch):
    monkeypatch.setattr(nc, "get_all_companies", lambda: [_co("Acme")])
    assert [r["company"] for r in nc.get_companies_missing_ats()] == ["Acme"]


def test_missing_env_var_names_the_env_file_and_not_the_value(monkeypatch):
    monkeypatch.delenv("SOME_MISSING_KEY", raising=False)
    with pytest.raises(RuntimeError) as err:
        nc._require_env("SOME_MISSING_KEY")
    assert ".env" in str(err.value) and "SOME_MISSING_KEY" in str(err.value)
    monkeypatch.setenv("SOME_PRESENT_KEY", "s3cret-value")
    assert nc._require_env("SOME_PRESENT_KEY") == "s3cret-value"


# --- #9: the sync writes Notion's verdict back onto the job ----------------

def test_sync_writes_is_new_back_onto_the_jobs():
    existing = ds.job(url="http://acme/old")
    fresh = ds.job(url="http://acme/new")
    ds.sync([ds.jrow(url="http://acme/old", pid="j-old")], [existing, fresh])
    assert existing["is_new"] is False
    assert fresh["is_new"] is True


# --- #5: the generic HTML scraper ------------------------------------------

def _fetch(html, company="Acme Medical", url="https://acme.com/careers/"):
    return html_generic.fetch_jobs(company, url, html=html)


def test_linkless_titles_are_skipped_not_collapsed_onto_the_careers_url(capsys):
    html = ('<a href="/jobs/1">Director of Sales</a>'
            '<a>VP of Marketing</a>'
            '<a href="#">Head of Quality</a>')
    jobs = _fetch(html)
    assert [j["title"] for j in jobs] == ["Director of Sales"]
    assert jobs[0]["url"] == "https://acme.com/jobs/1"
    assert "2 skipped (no link)" in capsys.readouterr().out


def test_titles_with_no_usable_link_at_all_raise_instead_of_reading_as_empty():
    with pytest.raises(ScrapeShapeError, match="none with a usable link"):
        _fetch('<a>Director of Sales</a><a href="javascript:x()">VP of Marketing</a>')


def test_dedup_is_on_url_not_title():
    html = ('<a href="/jobs/1">Director of Sales</a>'
            '<a href="/jobs/2">Director of Sales</a>'      # same title, other location
            '<a href="/jobs/1">Sales Director</a>')         # same URL, other wording
    jobs = _fetch(html)
    assert [j["url"] for j in jobs] == ["https://acme.com/jobs/1", "https://acme.com/jobs/2"]


def test_override_selector_finds_the_anchor_around_or_inside_the_element(monkeypatch):
    monkeypatch.setitem(html_generic.OVERRIDES, "Acme Medical", "h3.job")
    html = ('<a href="/jobs/1"><h3 class="job">Director of Sales</h3></a>'
            '<div><h3 class="job"><a href="/jobs/2">VP of Marketing</a></h3></div>')
    jobs = _fetch(html)
    assert {j["url"] for j in jobs} == {"https://acme.com/jobs/1", "https://acme.com/jobs/2"}


# --- #8: usage reporting ---------------------------------------------------

@pytest.fixture
def fresh_usage(monkeypatch):
    monkeypatch.setattr(score_jobs, "USAGE", {
        "calls": 0, "input_tokens": 0, "output_tokens": 0,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0})
    return score_jobs.USAGE


def test_usage_is_summed_from_each_response(fresh_usage):
    score_jobs._record_usage({"usage": {"input_tokens": 1000, "output_tokens": 200,
                                        "cache_read_input_tokens": 500}})
    score_jobs._record_usage({"usage": {"input_tokens": 10}})
    score_jobs._record_usage({})            # a reply with no usage block is tolerated
    assert fresh_usage["calls"] == 3 and fresh_usage["input_tokens"] == 1010
    assert fresh_usage["output_tokens"] == 200 and fresh_usage["cache_read_input_tokens"] == 500


def test_report_says_when_caching_is_not_firing(fresh_usage):
    assert "no Anthropic scoring calls" in score_jobs.usage_report()
    score_jobs._record_usage({"usage": {"input_tokens": 100, "output_tokens": 10}})
    assert "cache_control marker is being ignored" in score_jobs.usage_report()
    score_jobs._record_usage({"usage": {"cache_read_input_tokens": 100}})
    assert "caching IS firing" in score_jobs.usage_report()


def test_cost_estimate_weights_cache_reads_and_writes():
    u = {"input_tokens": 1_000_000, "output_tokens": 1_000_000,
         "cache_creation_input_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000}
    expected = 1.00 + 5.00 + 1.25 + 0.10
    assert score_jobs.estimated_cost_usd(u) == pytest.approx(expected)


# --- #17: run summary ------------------------------------------------------

def test_run_summary_has_the_fields_a_scheduled_run_needs():
    statuses = [{"row": {}, "status": run.SCRAPE_OK, "note": ""},
                {"row": {}, "status": run.SCRAPE_FAILED, "note": "x"},
                {"row": {}, "status": run.SCRAPE_FAILED, "note": "y"}]
    jobs = [{"is_new": True, "score": 4}, {"is_new": False, "score": None}]
    sync_result = {"created": 1, "updated": 0, "unchanged": 1, "closed": 0, "unflagged": 0,
                   "failed": 0, "left_open": 2, "left_open_unresolved": 0,
                   "close_aborted": False}
    s = run.build_run_summary(dry_run=False, companies=[1, 2, 3], scraped_ok={"a"},
                              statuses=statuses, jobs=jobs, sync_result=sync_result)
    assert s["companies_by_scrape_status"] == {run.SCRAPE_OK: 1, run.SCRAPE_FAILED: 2}
    assert s["jobs"] == 2 and s["jobs_new_in_notion"] == 1 and s["jobs_scored"] == 1
    assert s["notion"]["created"] == 1 and s["notion"]["left_open"] == 2
    assert "scoring_cost_usd_estimate" in s and "rubric_fingerprint" in s
    run.print_run_digest(s)          # must not raise on a real summary

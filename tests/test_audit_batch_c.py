"""Offline tests for audit batch C (punch list #32, #37, #39, #42, #44, #45, #46, #49).

Everything is stubbed: no network, no Notion, no Anthropic.
"""
import json
import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import http_client
import notion.client as nc
import run
from ats_finder import find_ats, find_website
from ats_finder.careers_url import ensure_scheme, is_usable_careers_url
from scrapers import html_generic, oracle_rc, successfactors
from scrapers.common import (ALL_POSTINGS_TERMS, SENIOR_SEARCH_TERMS,
                             ScrapeShapeError)
from scoring import score_jobs

import test_scrape_status as tss


# --- #32: a guess never overrides a researched platform --------------------

def _research(platform, monkeypatch, guess=("Greenhouse", "API")):
    info = {"ambiguous": False, "careers_url": None, "ats_platform": platform,
            "scrape_method": "API"}
    monkeypatch.setattr(find_ats, "find_via_search", lambda *a, **k: dict(info))
    monkeypatch.setattr(find_ats, "try_known_patterns", lambda name: guess)
    return find_ats.find_company_info("Acme")


def test_guess_does_not_overwrite_a_researched_platform(monkeypatch):
    assert _research("Lever", monkeypatch)["ats_platform"] == "Lever"
    assert _research("Workday", monkeypatch)["ats_platform"] == "Workday"


def test_guess_may_still_fill_an_unresolved_answer(monkeypatch):
    assert _research("Not found (auto-search failed)", monkeypatch)["ats_platform"] == "Greenhouse"
    assert _research("", monkeypatch)["ats_platform"] == "Greenhouse"


def _http_error(status):
    err = requests.HTTPError("HTTP %d" % status)
    err.response = type("R", (), {"status_code": status})()
    return err


def _guess_row(monkeypatch, first_error):
    calls = []

    def scraper(name, slug=None):
        calls.append(slug)
        if slug is not None:
            raise first_error
        return [{"title": "VP", "url": "http://x/1"}]

    monkeypatch.setitem(run.SCRAPERS, "Greenhouse", scraper)
    row = {"page_id": "pg-1", "company": "Acme", "tier": None, "sector": "s",
           "ats_platform": "Greenhouse", "scrape_method": "API",
           "careers_url": "https://boards.greenhouse.io/acme"}
    jobs, ok, statuses = run.run_scrapers([row])
    return calls, jobs, ok, statuses


def test_guessed_slug_is_tried_only_after_a_404(monkeypatch):
    calls, jobs, ok, statuses = _guess_row(monkeypatch, _http_error(404))
    assert calls == ["acme", None] and "pg-1" in ok and len(jobs) == 1


def test_timeout_or_5xx_on_the_saved_slug_does_not_guess(monkeypatch):
    for err in (requests.ConnectTimeout("t"), _http_error(503), _http_error(429)):
        calls, jobs, ok, statuses = _guess_row(monkeypatch, err)
        assert calls == ["acme"], calls
        assert "pg-1" not in ok and jobs == []
        assert statuses[0]["status"] == run.SCRAPE_FAILED


# --- #37: terms by tier for the URL-based platforms ------------------------

def _url_based_terms(monkeypatch, tier):
    seen = {}

    def fake(name, careers_url, search_terms=None):
        seen["terms"] = search_terms
        return []

    monkeypatch.setitem(run.SCRAPERS, "SuccessFactors", fake)
    row = {"page_id": "pg-1", "company": "Acme", "tier": tier, "sector": "s",
           "ats_platform": "SuccessFactors", "scrape_method": "API",
           "careers_url": "https://careers.acme.com/"}
    run.run_scrapers([row])
    return seen["terms"]


def test_untiered_company_reads_the_whole_board(monkeypatch):
    assert _url_based_terms(monkeypatch, None) == ALL_POSTINGS_TERMS == [""]


def test_tiered_company_keeps_the_senior_terms(monkeypatch):
    assert _url_based_terms(monkeypatch, "Large MedTech") == SENIOR_SEARCH_TERMS


def test_empty_keyword_is_sent_as_an_empty_query(monkeypatch):
    sent = []

    class R:
        status_code = 200
        headers = {}
        url = "https://careers.acme.com/search/"
        text = ""

        def raise_for_status(self):
            pass

    def fake_get(url, **kw):
        sent.append(kw["params"]["q"])
        return R()

    monkeypatch.setattr(successfactors, "session", type("S", (), {"get": staticmethod(fake_get)}))
    monkeypatch.setattr(successfactors, "_parse_page", lambda *a: ([], 0))
    monkeypatch.setattr(successfactors, "polite_sleep", lambda: None)
    successfactors.fetch_jobs("Acme", "https://careers.acme.com/", search_terms=ALL_POSTINGS_TERMS)
    assert sent == [""]


@pytest.mark.parametrize("module", [oracle_rc, successfactors])
def test_a_board_longer_than_max_pages_raises_instead_of_truncating(module, monkeypatch):
    monkeypatch.setattr(module, "MAX_PAGES", 2)
    monkeypatch.setattr(module, "polite_sleep", lambda: None)
    row = {"Id": "1", "Title": "VP", "PrimaryLocation": "X"}
    if module is oracle_rc:
        monkeypatch.setattr(module, "_get", lambda *a: {})
        monkeypatch.setattr(module, "_requisitions", lambda n, d: ([row], 999))
        url = "https://h.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1"
    else:
        class R:
            url, text = "https://c.x.com/search/", ""

            def raise_for_status(self):
                pass

        monkeypatch.setattr(module, "session",
                            type("S", (), {"get": staticmethod(lambda u, **k: R())}))
        monkeypatch.setattr(module, "_parse_page", lambda *a: ([object()], 999))
        monkeypatch.setattr(module, "_row_to_job", lambda *a: {"url": "u", "title": "t"})
        url = "https://c.x.com/"
    with pytest.raises(ScrapeShapeError, match="truncated"):
        module.fetch_jobs("Acme", url, search_terms=[""])


# --- #49 -------------------------------------------------------------------

def test_senior_terms_include_vp_and_svp():
    assert "vp" in SENIOR_SEARCH_TERMS and "svp" in SENIOR_SEARCH_TERMS


# --- #39: scorer output validation -----------------------------------------

def _entry(i, **kw):
    base = {"index": i, "score": 3, "reasoning": "r", "ambiguous": False,
            "ambiguity_note": "", "ic_role_flag": False, "routing": "Scored"}
    base.update(kw)
    return base


def test_duplicate_or_missing_indices_fail_the_batch():
    for bad in ([_entry(0), _entry(0)], [_entry(1), _entry(2)], [_entry(0), _entry(2)],
                [_entry(0), {"score": 3}]):
        with pytest.raises(score_jobs._BatchParseError):
            score_jobs._parse_json_array(json.dumps(bad), expected=2)


def test_out_of_order_but_complete_indices_are_sorted():
    out = score_jobs._parse_json_array(json.dumps([_entry(1, score=4), _entry(0, score=2)]), 2)
    assert [r["score"] for r in out] == [2, 4]


def test_extra_keys_are_dropped_from_a_result():
    out = score_jobs._parse_json_array(
        json.dumps([_entry(0, url="http://evil", title="Hijacked")]), 1)
    assert "url" not in out[0] and "title" not in out[0]


def test_scores_are_coerced_and_range_checked():
    assert score_jobs._coerce_score("3") == 3
    assert score_jobs._coerce_score(4.0) == 4
    assert score_jobs._coerce_score(0) == 0
    for bad in (6, -1, 2.5, "high", "", None, True, [3]):
        assert score_jobs._coerce_score(bad) is None, bad


def test_invalid_score_makes_that_posting_a_failure_not_a_scored_row():
    out = score_jobs._parse_json_array(json.dumps([_entry(0, score="n/a"), _entry(1)]), 2)
    assert out[0]["score"] is None and "invalid score" in out[0]["reasoning"]
    assert out[1]["score"] == 3
    # and the failure is not cached as an answer: score None is always re-scored
    assert score_jobs._enforce_routing(out[0])["routing"] == "Needs review"


def test_a_cached_result_cannot_overwrite_the_jobs_identity(monkeypatch, tmp_path):
    monkeypatch.setattr(score_jobs, "CACHE_PATH", str(tmp_path / "c.json"))
    job = {"url": "http://real/1", "title": "Real", "company": "Acme"}
    key = score_jobs._job_key(job)
    cache = {key: dict(_entry(0), url="http://evil", title="Hijacked",
                       rubric_fp=score_jobs.RUBRIC_FINGERPRINT)}
    score_jobs._save_cache(cache)
    scored = score_jobs.score_all([job], dry_run=True)
    assert scored[0]["url"] == "http://real/1" and scored[0]["title"] == "Real"
    assert scored[0]["score"] == 3


# --- #42: scrape note comparison -------------------------------------------

CONN_NOTE = ("HTML scrape failed: HTTPSConnectionPool(host='x.com'): Max retries exceeded "
             "(Caused by NameResolutionError(\"<urllib3.connection.HTTPSConnection object at "
             "0x000001F3A9B2C710>: Failed to resolve\"))")


def test_object_address_is_stripped_and_long_notes_truncated():
    cleaned = nc._normalise_scrape_note(CONN_NOTE)
    assert "0x" not in cleaned
    assert len(nc._normalise_scrape_note("x" * 5000)) == nc.SCRAPE_NOTE_MAX


def test_connection_error_note_is_not_rewritten_every_run():
    run1 = CONN_NOTE
    run2 = CONN_NOTE.replace("0x000001F3A9B2C710", "0x000002AA11BB22CC")
    stored = nc._normalise_scrape_note(run1)          # what Notion holds after run 1
    row = {"page_id": "p", "company": "Acme", "scrape_status": nc.SCRAPE_FAILED,
           "scrape_note": stored, "failing_since": "2026-11-01"}
    res, writes = tss.stamp([{"row": row, "status": nc.SCRAPE_FAILED, "note": run2}])
    assert writes == [] and res["unchanged"] == 1


def test_over_long_note_is_not_rewritten_every_run():
    long = "y" * 3000
    row = {"page_id": "p", "company": "Acme", "scrape_status": nc.SCRAPE_FAILED,
           "scrape_note": long[:2000], "failing_since": "2026-11-01"}
    res, writes = tss.stamp([{"row": row, "status": nc.SCRAPE_FAILED, "note": long}])
    assert writes == []


# --- #44: scheme-less URLs --------------------------------------------------

def test_ensure_scheme_is_narrow():
    assert ensure_scheme("example.com/careers") == "https://example.com/careers"
    assert ensure_scheme("careers.example.co.uk") == "https://careers.example.co.uk"
    for untouched in ("N/A", "", None, "https://x.com", "Not found. See x.com", "phoenixkinetics",
                      "n.a."):
        assert ensure_scheme(untouched) == untouched, untouched
    assert is_usable_careers_url(ensure_scheme("example.com/careers"))


def test_research_reply_with_a_scheme_less_url_is_saved_with_one(monkeypatch):
    text = ("AMBIGUOUS: false\nSECTOR: medtech\nHQ: Boston\nATS: Greenhouse\n"
            "URL: example.com/careers\nHOMEPAGE: example.com\n")
    info = _parse_reply(text, monkeypatch)
    assert info["careers_url"] == "https://example.com/careers"


def _parse_reply(text, monkeypatch):
    class Resp:
        ok = True

        def json(self):
            return {"stop_reason": "end_turn", "content": [{"type": "text", "text": text}]}

    monkeypatch.setattr(find_ats, "ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(find_ats, "session", type("S", (), {"post": lambda *a, **k: Resp()})())
    return find_ats.find_via_search("Example Co")


def test_scheme_less_homepage_answer_yields_a_host(monkeypatch):
    class Resp:
        ok = True

        def json(self):
            return {"content": [{"type": "text", "text": "example.com"}]}

    monkeypatch.setattr(find_website, "ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(find_website, "session", type("S", (), {"post": lambda *a, **k: Resp()})())
    homepage = find_website.find_website_homepage("Example Co")
    assert homepage == "https://example.com"
    assert find_website._real_company_host(homepage) == "example.com"


# --- #45: encoding and hrefs -------------------------------------------------

class _Page:
    def __init__(self, content_type, apparent, raw):
        self.headers = {"content-type": content_type}
        self.apparent_encoding = apparent
        self.encoding = "ISO-8859-1"       # what requests assumes with no charset
        self._raw = raw

    @property
    def text(self):
        return self._raw.decode(self.encoding)


def test_undeclared_charset_uses_the_detected_one():
    raw = "PA · Full-time".encode("utf-8")
    page = _Page("text/html", "utf-8", raw)
    assert http_client.response_text(page) == "PA · Full-time"


def test_declared_charset_is_trusted():
    raw = "café".encode("latin-1")
    page = _Page("text/html; charset=ISO-8859-1", "utf-8", raw)
    assert http_client.response_text(page) == "café"


def test_hrefs_are_joined_and_non_links_dropped():
    base = "https://acme.com/company/careers/"
    n = html_generic._normalise_href
    assert n("/jobs/1", base) == "https://acme.com/jobs/1"
    assert n("jobs/2", base) == "https://acme.com/company/careers/jobs/2"
    assert n("https://other.com/x", base) == "https://other.com/x"
    for non_link in ("#", "#apply", "javascript:void(0)", "mailto:hr@acme.com", "tel:555", "", None):
        assert n(non_link, base) is None, non_link


def test_generic_scrape_uses_joined_links_and_falls_back_for_non_links():
    html = ('<a href="jobs/7">Director of Sales</a>'
            '<a href="javascript:go()">VP of Marketing</a>')
    soup = html_generic.BeautifulSoup(html, "html.parser")
    titles, links = html_generic._generic_extract(soup, "https://acme.com/careers/")
    assert links[0] == "https://acme.com/careers/jobs/7"
    assert links[1] is None, "javascript: is not a link; fetch_jobs falls back to the careers URL"


# --- #46: the sniff fetches through the third-party session ----------------

def test_sniff_fetches_through_the_third_party_session(monkeypatch):
    seen = []

    class R:
        status_code = 200
        history = []
        url = "https://careers.acme.com/"
        text = "<html>nothing</html>"

    class S:
        def get(self, url, **kw):
            seen.append(url)
            return R()

    monkeypatch.setattr(find_ats, "third_party_session", S())
    monkeypatch.setattr(find_ats, "_guess_session", None)   # would blow up if used
    assert find_ats.sniff_ats_from_page("https://careers.acme.com/") is None
    assert seen == ["https://careers.acme.com/"]

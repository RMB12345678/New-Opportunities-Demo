"""Offline tests for audit batch B (punch list #30, #34, #35, #38, #43).

Row identity: each of these is a way a posting ends up with two identities
or a company's rows get closed on another company's say-so. Everything is
stubbed; nothing touches the network or the real output/ directory (conftest
points the carry file at a temp dir).
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import http_client
import notion.client as nc
import run
from scrapers import smartrecruiters, personio
from scrapers.common import ScrapeShapeError

import test_close_pass as cp
import test_delta_sync as ds
from test_new_scrapers import FakeResponse, stub, restore, PERSONIO_XML


# Other test modules replace nc._create_job_posting wholesale and never put it
# back, so grab the real one at import (collection) time, before any test runs.
_REAL_CREATE_JOB_POSTING = nc._create_job_posting


# --- #30: SmartRecruiters URL is always the detail call's URL --------------

def _sr_handler(total, fail_ids=(), no_url_ids=()):
    def handler(url, **kwargs):
        if "/postings/" in url:
            pid = url.rsplit("/", 1)[1]
            if pid in fail_ids:
                return FakeResponse(status=500)
            body = {"jobAd": {"sections": {}}}
            if pid not in no_url_ids:
                body["postingUrl"] = f"https://jobs.smartrecruiters.com/Acme/{pid}-title"
            return FakeResponse(body)
        offset, limit = kwargs["params"]["offset"], kwargs["params"]["limit"]
        content = [{"id": str(i), "name": "J%d" % i, "location": {}}
                   for i in range(offset, min(offset + limit, total))]
        return FakeResponse({"totalFound": total, "content": content})
    return handler


def test_smartrecruiters_126_postings_all_use_the_detail_url():
    """Brainlab: 126 postings, formerly 26 of them on the bare-id fallback."""
    stub(smartrecruiters, _sr_handler(126))
    try:
        jobs = smartrecruiters.fetch_jobs("Acme", slug="acme")
    finally:
        restore(smartrecruiters)
    assert len(jobs) == 126
    assert all(j["url"].endswith("-title") for j in jobs)


def test_smartrecruiters_detail_failure_fails_the_company():
    stub(smartrecruiters, _sr_handler(5, fail_ids={"3"}))
    try:
        with pytest.raises(RuntimeError, match="detail call"):
            smartrecruiters.fetch_jobs("Acme", slug="acme")
    finally:
        restore(smartrecruiters)


def test_smartrecruiters_detail_without_a_url_fails_the_company():
    stub(smartrecruiters, _sr_handler(3, no_url_ids={"1"}))
    try:
        with pytest.raises(ScrapeShapeError):
            smartrecruiters.fetch_jobs("Acme", slug="acme")
    finally:
        restore(smartrecruiters)


def test_smartrecruiters_board_over_the_cap_raises(monkeypatch):
    monkeypatch.setattr(smartrecruiters, "MAX_DETAIL_FETCHES", 3)
    stub(smartrecruiters, _sr_handler(5))
    try:
        with pytest.raises(ScrapeShapeError):
            smartrecruiters.fetch_jobs("Acme", slug="acme")
    finally:
        restore(smartrecruiters)


# --- #34: creates are not retried on 5xx or read timeouts ------------------

def test_create_session_retries_only_429_and_connect_errors():
    retry = http_client.create_session.get_adapter("https://api.notion.com").max_retries
    assert list(retry.status_forcelist) == [429]
    assert retry.read == 0, "a read timeout has an unknown outcome and must not be re-sent"
    assert retry.is_retry("POST", 429)
    assert not retry.is_retry("POST", 502)
    assert not retry.is_retry("POST", 504)
    # the shared session still retries them, which is why creates left it
    shared = http_client.session.get_adapter("https://api.notion.com").max_retries
    assert shared.is_retry("POST", 502)


def test_create_job_posting_posts_through_create_session(monkeypatch):
    seen = []

    class Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"id": "p1"}

        def raise_for_status(self):
            pass

    class Fake:
        def post(self, url, **kw):
            seen.append(url)
            return Resp()

    monkeypatch.setattr(nc, "create_session", Fake())
    monkeypatch.setattr(nc, "session", None)  # any use of the shared one would blow up
    monkeypatch.setattr(nc.time, "sleep", lambda s: None)
    monkeypatch.setattr(nc, "_raise_with_detail", lambda r: None)
    assert _REAL_CREATE_JOB_POSTING({"title": "x"}) == {"id": "p1"}
    assert seen and seen[0].endswith("/pages")


# --- #35: company identity is the page id, not the name --------------------

TWINS = [{"page_id": "pg-a", "company": "Neuvotion"},
         {"page_id": "pg-b", "company": "Neuvotion"}]


def test_prepare_jobs_stamps_the_page_id():
    jobs = run.prepare_jobs({"page_id": "pg-a", "company": "Neuvotion", "sector": "s"},
                            [{"title": "VP"}])
    assert jobs[0]["company_page_id"] == "pg-a"


def test_scraped_ok_holds_page_ids(monkeypatch):
    monkeypatch.setitem(run.SCRAPERS, "Greenhouse",
                        lambda name, slug=None: [{"title": "VP", "url": "http://x/1"}])
    row = {"page_id": "pg-a", "company": "Neuvotion", "tier": None, "sector": "s",
           "ats_platform": "Greenhouse", "scrape_method": "API",
           "careers_url": "https://boards.greenhouse.io/neuvotion"}
    _, ok, _ = run.run_scrapers([row])
    assert ok == {"pg-a"}


def _twin_sync(existing, jobs, scraped_ok):
    closed, writes, creates = [], [], []
    nc._query_all_target_list_pages = lambda: [dict(t) for t in TWINS]
    names = {t["page_id"]: t["company"] for t in TWINS}
    nc._query_all_job_postings = lambda lookup=None: [
        dict(r, company=names.get(r["company_page_id"])) for r in existing]

    def fake_update(pid, props, icon_url=None, dry_run=False):
        if props.get("Still Open") == "__NO__":
            closed.append(pid)
        else:
            writes.append(pid)

    def fake_create(props, icon_url=None, dry_run=False):
        creates.append(props)
        return {"id": "new"}

    nc._update_job_posting = fake_update
    nc._create_job_posting = fake_create
    nc.state = ds._StubState()
    nc.sync_jobs_to_notion(jobs, scraped_ok, rubric_fingerprint="fp")
    return closed, creates


def test_failed_twin_is_not_closed_by_its_successful_namesake():
    """pg-b failed; pg-a scraped OK with zero jobs. By name, 'Neuvotion' was
    in scraped_ok and pg-b's rows closed."""
    existing = [cp.row("a1", "http://x/a1", "pg-a"), cp.row("b1", "http://x/b1", "pg-b")]
    closed, _ = _twin_sync(existing, [], scraped_ok={"pg-a"})
    assert closed == ["a1"], closed


def test_job_relation_uses_the_jobs_own_page_id():
    job = {"url": "http://x/new", "company": "Neuvotion", "company_page_id": "pg-b",
           "title": "VP", "score": 5, "routing": "Scored", "source_ats": "Greenhouse"}
    _, creates = _twin_sync([], [job], scraped_ok={"pg-b"})
    assert creates[0]["Company"] == ["pg-b"]


# --- #38: created ids survive an interrupted sync --------------------------

def _stub_sync_world(fake_create):
    nc._query_all_target_list_pages = lambda: [dict(ds.TARGETS[0])]
    nc._query_all_job_postings = lambda lookup=None: []
    nc._update_job_posting = lambda *a, **k: None
    nc._create_job_posting = fake_create
    nc.state = ds._StubState()


def _jobs(n):
    return [ds.job(url="http://acme/%d" % i) for i in range(n)]


def _numbered_create(crash_after=None):
    n = {"c": 0}

    def fake_create(props, icon_url=None, dry_run=False):
        if dry_run:
            return None
        if crash_after is not None and n["c"] >= crash_after:
            raise KeyboardInterrupt
        n["c"] += 1
        return {"id": "page-%d" % n["c"]}

    return fake_create


def test_interrupted_sync_leaves_the_created_ids_behind():
    _stub_sync_world(_numbered_create(crash_after=3))
    with pytest.raises(KeyboardInterrupt):
        nc.sync_jobs_to_notion(_jobs(5), frozenset({"pg-acme"}), rubric_fingerprint="fp")
    with open(nc.CARRY_NEW_PATH, encoding="utf-8") as f:
        assert json.load(f) == ["page-1", "page-2", "page-3"]


def test_completed_sync_deletes_the_carry_file():
    _stub_sync_world(_numbered_create())
    nc.sync_jobs_to_notion(_jobs(3), frozenset({"pg-acme"}), rubric_fingerprint="fp")
    assert not os.path.exists(nc.CARRY_NEW_PATH)


def test_dry_run_writes_no_carry_file():
    _stub_sync_world(_numbered_create())
    nc.sync_jobs_to_notion(_jobs(2), frozenset({"pg-acme"}), dry_run=True,
                           rubric_fingerprint="fp")
    assert not os.path.exists(nc.CARRY_NEW_PATH)


def test_carried_ids_from_an_earlier_run_are_kept_if_this_one_is_interrupted():
    nc._write_carry_file({"old-1"})
    _stub_sync_world(_numbered_create(crash_after=1))
    with pytest.raises(KeyboardInterrupt):
        nc.sync_jobs_to_notion(_jobs(3), frozenset({"pg-acme"}), carry_new_ids=["old-1"],
                               rubric_fingerprint="fp")
    with open(nc.CARRY_NEW_PATH, encoding="utf-8") as f:
        assert json.load(f) == ["old-1", "page-1"]


# --- #43: Personio falls to .de only on a "no such board" answer -----------

def _personio(handler):
    stub(personio, handler)
    try:
        return personio.fetch_jobs("Acme", slug="acme")
    finally:
        restore(personio)


def test_personio_timeout_on_com_does_not_fall_to_de():
    seen = []

    def handler(url, **kw):
        seen.append(url)
        raise TimeoutError("read timed out")

    with pytest.raises(TimeoutError):
        _personio(handler)
    assert len(seen) == 1 and "personio.com" in seen[0], seen


def test_personio_5xx_on_com_does_not_fall_to_de():
    seen = []

    def handler(url, **kw):
        seen.append(url)
        return FakeResponse(status=503)

    with pytest.raises(RuntimeError, match="503"):
        _personio(handler)
    assert len(seen) == 1


def test_personio_redirect_on_com_still_falls_to_de():
    def handler(url, **kw):
        if "personio.com" in url:
            return FakeResponse(status=307, location="https://personio.com")
        return FakeResponse(content=PERSONIO_XML)

    jobs = _personio(handler)
    assert jobs[0]["url"].endswith("personio.de/job/2793912")

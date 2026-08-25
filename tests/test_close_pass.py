"""Tests for the Job Postings close pass (punch list #2).

The bug these exist to prevent: the close pass used to treat "this URL did
not appear in this run" as proof the posting was gone. Since run_scrapers()
catches every scrape exception, a company that timed out contributed zero
URLs and looked exactly like a company with nothing open, so all its live
roles were marked closed. They never came back either — the next successful
run found their URLs already in Notion and took the update branch.

Every test here stubs the Notion HTTP layer, so the suite makes no network
calls, spends nothing, and needs no credentials.

Runs under pytest, and standalone (`python tests/test_close_pass.py`) for
the case where pytest isn't installed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("NOTION_API_KEY", "test-key-not-real")
os.environ.setdefault("NOTION_DATABASE_ID", "test-target-list-db")
os.environ.setdefault("JOB_POSTINGS_DATABASE_ID", "test-job-postings-db")

import notion.client as nc

ACME = "Acme Medical"
BETA = "Beta Surgical"
TARGETS = [
    {"page_id": "pg-acme", "company": ACME},
    {"page_id": "pg-beta", "company": BETA},
]


def row(pid, url, company_page_id, still_open=True, title="A Role"):
    return {"page_id": pid, "url": url, "still_open": still_open,
            "title": title, "company_page_id": company_page_id}


def filler(n, company_page_id="pg-acme"):
    """n open rows that DO show up in the run, purely to pad the open-row
    count past the valve's floor so a test can exercise the ratio. Returns
    (rows, jobs) so a caller can splice both in."""
    rows = [row("f%d" % i, "http://filler/%d" % i, company_page_id) for i in range(n)]
    return rows, [{"url": r["url"]} for r in rows]


def sync(existing_rows, jobs, scraped_ok, dry_run=False):
    """Run sync_jobs_to_notion against stubbed Notion queries.

    Returns (result_dict, closed_page_ids). closed_page_ids records only
    writes that actually set Still Open = false: the normal update branch
    calls the same function, and counting those as closures made an earlier
    version of this harness report passes that weren't real.
    """
    closed_ids = []

    def fake_update(page_id, props, dry_run=False):
        # Mirror the real _update_job_posting: under dry_run it returns
        # before touching the network, so nothing is recorded here either.
        if dry_run:
            return None
        if props.get("Still Open") == "__NO__":
            closed_ids.append(page_id)

    name_by_id = {t["page_id"]: t["company"] for t in TARGETS}
    rows = [dict(r, company=name_by_id.get(r["company_page_id"])) for r in existing_rows]

    nc._query_all_target_list_pages = lambda: list(TARGETS)
    nc._query_all_job_postings = lambda lookup=None: [dict(r) for r in rows]
    nc._update_job_posting = fake_update
    nc._create_job_posting = lambda props, dry_run=False: None

    return nc.sync_jobs_to_notion(jobs, scraped_ok, dry_run=dry_run), closed_ids


def test_successful_scrape_closes_missing_row():
    pad, padjobs = filler(30)
    res, closed = sync(
        [row("j1", "http://acme/1", "pg-acme"),
         row("j2", "http://acme/2", "pg-acme")] + pad,
        [{"url": "http://acme/1"}] + padjobs,
        scraped_ok={ACME})
    assert res["closed"] == 1
    assert closed == ["j2"]


def test_failed_scrape_leaves_its_rows_open():
    """The core regression. Beta raised during scraping, so it is absent
    from scraped_ok and none of its rows may be closed."""
    pad, padjobs = filler(30)
    res, closed = sync(
        [row("j1", "http://acme/1", "pg-acme"),
         row("j2", "http://acme/2", "pg-acme"),
         row("j3", "http://beta/1", "pg-beta"),
         row("j4", "http://beta/2", "pg-beta")] + pad,
        [{"url": "http://acme/1"}] + padjobs,
        scraped_ok={ACME})
    assert closed == ["j2"], "only Acme's missing row may close"
    assert res["left_open"] == 2
    assert not res["close_aborted"]


def test_total_scrape_failure_closes_nothing():
    """Nothing scraped means nothing may be concluded. Pre-fix this closed
    every row in the database."""
    res, closed = sync(
        [row("j1", "http://acme/1", "pg-acme"),
         row("j2", "http://beta/1", "pg-beta")],
        [],
        scraped_ok=set())
    assert res["closed"] == 0
    assert res["left_open"] == 2
    assert closed == []


def test_unresolved_company_relation_never_closes():
    """A row with no Company relation, or one pointing at a deleted Target
    List page, has unknown provenance — we cannot confirm its scrape
    succeeded, so it stays open and gets counted separately."""
    pad, padjobs = filler(30)
    res, closed = sync(
        [row("j1", "http://acme/1", "pg-acme"),
         row("j2", "http://acme/2", "pg-acme"),
         row("j3", "http://ghost/1", None),
         row("j4", "http://ghost/2", "pg-deleted-company")] + pad,
        [{"url": "http://acme/1"}] + padjobs,
        scraped_ok={ACME})
    assert closed == ["j2"]
    assert res["left_open_unresolved"] == 2


def test_zero_jobs_from_a_reachable_company_still_closes():
    """Zero jobs is a real answer, not a failure: the company was reachable
    and genuinely has nothing open, so its old rows should close."""
    pad, padjobs = filler(30, "pg-beta")
    res, closed = sync(
        [row("j1", "http://acme/1", "pg-acme")] + pad,
        padjobs,
        scraped_ok={ACME, BETA})
    assert res["closed"] == 1
    assert closed == ["j1"]


def test_valve_aborts_whole_pass_above_the_floor():
    pad, padjobs = filler(8, "pg-beta")
    doomed = [row("d%d" % i, "http://acme/%d" % i, "pg-acme") for i in range(24)]
    res, closed = sync(doomed + pad, padjobs, scraped_ok={ACME, BETA})
    assert res["close_aborted"]
    assert res["closed"] == 0
    assert closed == [], "an aborted pass must close nothing at all, not a subset"


def test_valve_allows_exactly_the_limit():
    """The limit is *more than* 25%, so exactly 25% goes through."""
    pad, padjobs = filler(18, "pg-beta")
    doomed = [row("d%d" % i, "http://acme/%d" % i, "pg-acme") for i in range(6)]
    res, _ = sync(doomed + pad, padjobs, scraped_ok={ACME, BETA})
    assert not res["close_aborted"]
    assert res["closed"] == 6


def test_valve_floor_lets_small_databases_close():
    """Without the floor, 3 of 4 rows is 75% and aborts — and aborts again
    on every subsequent run, so those rows could never close at all."""
    res, _ = sync(
        [row("j%d" % i, "http://acme/%d" % i, "pg-acme") for i in range(4)],
        [{"url": "http://acme/3"}],
        scraped_ok={ACME})
    assert not res["close_aborted"]
    assert res["closed"] == 3


def test_valve_floor_boundary():
    """19 open rows is under the floor and closes; 20 is at the floor, so
    the ratio applies and aborts."""
    under = [row("d%d" % i, "http://acme/%d" % i, "pg-acme") for i in range(19)]
    res, _ = sync(under, [], scraped_ok={ACME})
    assert not res["close_aborted"] and res["closed"] == 19

    at = [row("d%d" % i, "http://acme/%d" % i, "pg-acme") for i in range(20)]
    res, _ = sync(at, [], scraped_ok={ACME})
    assert res["close_aborted"] and res["closed"] == 0


def test_empty_database_does_not_divide_by_zero():
    res, _ = sync([], [], scraped_ok=set())
    assert res["closed"] == 0
    assert not res["close_aborted"]


def test_dry_run_reports_without_writing():
    res, closed = sync(
        [row("j1", "http://acme/1", "pg-acme", title="Closing Role"),
         row("j2", "http://beta/1", "pg-beta", title="Protected Role"),
         row("j3", "http://ghost/1", None, title="Orphan Role")],
        [{"url": "http://acme/2"}],
        scraped_ok={ACME}, dry_run=True)
    assert closed == [], "a dry run must not write"
    assert res["closed"] == 1, "but it must still report the would-close"
    assert res["left_open"] == 1
    assert res["left_open_unresolved"] == 1


if __name__ == "__main__":
    # Standalone runner, for when pytest isn't installed.
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print("PASS  %s" % name)
        except AssertionError as e:
            failures += 1
            print("FAIL  %s: %s" % (name, e))
    print("\n%d passed, %d failed" % (
        len([n for n in globals() if n.startswith("test_")]) - failures, failures))
    sys.exit(1 if failures else 0)

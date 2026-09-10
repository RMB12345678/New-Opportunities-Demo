"""Tests for the Scrape Status stamp written back to the Target List.

These back the "Needs Manual Check" view in Notion. That view is only as
good as the stamp behind it, and the stamp has three properties worth
protecting:

  - It has to mark every company no scraper could reach, or a broken
    company silently disappears from the one place it would be noticed.
  - It has to leave `Failing Since` alone while a company keeps failing.
    Re-stamping it every run turns "broken since August" into "broken
    since today", which is the single fact the view exists to show.
  - It has to skip rows that haven't changed. A healthy run has several
    hundred companies scraping fine, and rewriting all of them every run
    is minutes of Notion rate limit spent to change nothing.

Like the close-pass tests, everything here stubs the Notion HTTP layer, so
the suite makes no network calls, spends nothing, and needs no credentials.

Runs under pytest, and standalone (`python tests/test_scrape_status.py`)
for the case where pytest isn't installed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("NOTION_API_KEY", "test-key-not-real")
os.environ.setdefault("NOTION_DATABASE_ID", "test-target-list-db")
os.environ.setdefault("JOB_POSTINGS_DATABASE_ID", "test-job-postings-db")

import notion.client as nc

TODAY = __import__("datetime").date.today().isoformat()


def company(name="Acme Medical", status=None, note=None, failing_since=None):
    """A Target List row as get_all_companies() returns it. The three stamp
    fields default to None, which is what an unstamped row really looks
    like — every company was in that state before this feature landed."""
    return {"page_id": "pg-" + name.lower().replace(" ", "-"), "company": name,
            "scrape_status": status, "scrape_note": note, "failing_since": failing_since}


def stamp(records, dry_run=False):
    """Run sync_scrape_status with the page write stubbed out.

    Returns (result_dict, writes) where writes is one dict per row that
    actually reached the writer — the whole point of several tests below is
    that unchanged rows never get there.
    """
    writes = []

    def fake_write(page_id, status, note, failing_since, dry_run=False):
        # Mirror the real _write_scrape_status: under dry_run it returns
        # before the network call, so nothing is recorded here either.
        if dry_run:
            return None
        writes.append({"page_id": page_id, "status": status,
                        "note": note, "failing_since": failing_since})

    original = nc._write_scrape_status
    nc._write_scrape_status = fake_write
    try:
        return nc.sync_scrape_status(records, dry_run=dry_run), writes
    finally:
        nc._write_scrape_status = original


def test_failure_is_stamped_and_dated():
    row = company()
    res, writes = stamp([{"row": row, "status": nc.SCRAPE_FAILED, "note": "404 from board"}])
    assert res["written"] == 1
    assert writes[0]["status"] == nc.SCRAPE_FAILED
    assert writes[0]["note"] == "404 from board"
    assert writes[0]["failing_since"] == TODAY, "a newly broken company is broken as of today"


def test_every_non_ok_status_reaches_notion():
    """All three failure shapes are manual-check cases, not just Failed."""
    rows = [company("A"), company("B"), company("C")]
    res, writes = stamp([
        {"row": rows[0], "status": nc.SCRAPE_FAILED, "note": "timeout"},
        {"row": rows[1], "status": nc.SCRAPE_NO_SCRAPER, "note": "Workday"},
        {"row": rows[2], "status": nc.SCRAPE_NO_URL, "note": "no url saved"},
    ])
    assert res["written"] == 3
    assert {w["status"] for w in writes} == {
        nc.SCRAPE_FAILED, nc.SCRAPE_NO_SCRAPER, nc.SCRAPE_NO_URL}
    assert all(w["failing_since"] == TODAY for w in writes)


def test_still_failing_keeps_the_original_date():
    """The company broke in August. Today's run must not relabel it as a
    problem that started today — that erases exactly the signal the view is
    sorted by."""
    row = company(status=nc.SCRAPE_FAILED, note="404 from board", failing_since="2026-08-01")
    res, writes = stamp([{"row": row, "status": nc.SCRAPE_FAILED, "note": "404 from board"}])
    assert res["unchanged"] == 1, "nothing moved, so nothing should be written"
    assert writes == []


def test_still_failing_with_a_new_reason_rewrites_the_note_but_not_the_date():
    row = company(status=nc.SCRAPE_FAILED, note="404 from board", failing_since="2026-08-01")
    res, writes = stamp([{"row": row, "status": nc.SCRAPE_FAILED, "note": "connection reset"}])
    assert res["written"] == 1
    assert writes[0]["note"] == "connection reset"
    assert writes[0]["failing_since"] == "2026-08-01", "still the same outage"


def test_status_change_between_failure_shapes_keeps_the_date():
    """No Careers URL becoming Failed is the same unbroken stretch of not
    working, not a fresh problem."""
    row = company(status=nc.SCRAPE_NO_URL, note="no url saved", failing_since="2026-08-01")
    res, writes = stamp([{"row": row, "status": nc.SCRAPE_FAILED, "note": "404"}])
    assert writes[0]["status"] == nc.SCRAPE_FAILED
    assert writes[0]["failing_since"] == "2026-08-01"


def test_recovery_clears_the_date():
    row = company(status=nc.SCRAPE_FAILED, note="404 from board", failing_since="2026-08-01")
    res, writes = stamp([{"row": row, "status": nc.SCRAPE_OK, "note": "Greenhouse: 4 job(s)"}])
    assert res["written"] == 1
    assert writes[0]["status"] == nc.SCRAPE_OK
    assert writes[0]["failing_since"] is None, "a working company has no failing-since date"


def test_breaking_again_after_recovery_dates_from_today():
    row = company(status=nc.SCRAPE_OK, note="Greenhouse: 4 job(s)", failing_since=None)
    res, writes = stamp([{"row": row, "status": nc.SCRAPE_FAILED, "note": "404"}])
    assert writes[0]["failing_since"] == TODAY


def test_unchanged_healthy_rows_are_not_rewritten():
    """The volume case: a steady run must not PATCH 300 rows to write the
    values already sitting in them."""
    rows = [company("Co %d" % i, status=nc.SCRAPE_OK, note="Greenhouse: 2 job(s)")
            for i in range(300)]
    res, writes = stamp([{"row": r, "status": nc.SCRAPE_OK, "note": "Greenhouse: 2 job(s)"}
                          for r in rows])
    assert res["unchanged"] == 300
    assert res["written"] == 0
    assert writes == []


def test_job_count_change_does_rewrite():
    """The note carries the job count, so a company going 2 -> 3 jobs is a
    real change and should be stamped. This is the cost of putting the count
    in the note, and it is worth knowing it is deliberate."""
    row = company(status=nc.SCRAPE_OK, note="Greenhouse: 2 job(s)")
    res, writes = stamp([{"row": row, "status": nc.SCRAPE_OK, "note": "Greenhouse: 3 job(s)"}])
    assert res["written"] == 1


def test_first_stamp_on_a_never_stamped_healthy_row():
    """Every row was unstamped before this feature existed, so the first run
    after it lands writes all of them once. It must not then keep writing
    them on every later run."""
    row = company()
    res, writes = stamp([{"row": row, "status": nc.SCRAPE_OK, "note": "Greenhouse: 2 job(s)"}])
    assert res["written"] == 1
    assert writes[0]["failing_since"] is None


def test_a_single_write_failure_does_not_stop_the_rest():
    """One row's PATCH failing must not cost the other 335 their stamp, or
    the view is wrong for a whole run over one transient error."""
    def exploding(page_id, status, note, failing_since, dry_run=False):
        if page_id == "pg-b":
            raise RuntimeError("Notion said no")

    original = nc._write_scrape_status
    nc._write_scrape_status = exploding
    try:
        res = nc.sync_scrape_status([
            {"row": company("A"), "status": nc.SCRAPE_FAILED, "note": "x"},
            {"row": company("B"), "status": nc.SCRAPE_FAILED, "note": "y"},
            {"row": company("C"), "status": nc.SCRAPE_FAILED, "note": "z"},
        ])
    finally:
        nc._write_scrape_status = original

    assert res["failed"] == 1
    assert res["written"] == 2, "the other two still got stamped"


def test_dry_run_writes_nothing():
    row = company()
    res, writes = stamp(
        [{"row": row, "status": nc.SCRAPE_FAILED, "note": "404"}], dry_run=True)
    assert writes == [], "a dry run must not write"
    assert res["written"] == 1, "but it must still report the would-write"


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

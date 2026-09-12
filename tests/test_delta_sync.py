"""Tests for delta-only syncing of Job Postings.

The problem these lock in: sync_jobs_to_notion() used to PATCH all twelve
owned properties onto every job on every run, whether or not anything had
moved. At ~3,200 postings that was ~3,200 writes a run, each carrying a
0.35s throttle sleep, so the sync alone ran about half an hour — and wrote
byte-identical values to roughly 3,150 of those rows.

The fix compares each row against what Notion already holds and writes only
the difference. That makes a whole class of subtle bug possible that simply
could not exist before, because a value that compares unequal *forever*
looks exactly like a value that legitimately changed: the row is rewritten
every single run and the saving quietly evaporates. Most of the tests below
exist for that failure mode specifically —

  - Notion returns None for an empty rich_text where the scraper returns ""
  - Notion returns a Score of 7.0 where the scorer produced 7
  - _build_job_properties() truncates text at 2000 chars on write, so a
    longer value can never read back equal to the untruncated original

Each of those would have rewritten the entire table on every run while the
sync reported itself as working normally.

Everything here stubs the Notion HTTP layer and notion.state, so the suite
makes no network calls, spends nothing, needs no credentials, and never
touches the real output/notion_state.json.

Runs under pytest, and standalone (`python tests/test_delta_sync.py`) for
the case where pytest isn't installed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("NOTION_API_KEY", "test-key-not-real")
os.environ.setdefault("NOTION_DATABASE_ID", "test-target-list-db")
os.environ.setdefault("JOB_POSTINGS_DATABASE_ID", "test-job-postings-db")

import notion.client as nc

TODAY = __import__("datetime").date.today().isoformat()
PREVIOUS_RUN = "2026-09-08"

ACME = "Acme Medical"
TARGETS = [{"page_id": "pg-acme", "company": ACME}]


class _StubState:
    """Stands in for notion.state. A test must never read or write the real
    output/notion_state.json: advancing the recorded last_run to today would
    make the next REAL run stamp closing postings with the date of a test
    rather than the date they were last actually seen."""

    def __init__(self):
        self.saved = []

    def load(self, path=None):
        return {"version": 1, "rubric_fingerprint": "fp-old",
                "last_run": PREVIOUS_RUN, "last_run_counts": {}}

    def save(self, fingerprint, counts=None, path=None, dry_run=False):
        if not dry_run:
            self.saved.append((fingerprint, counts))


def jrow(url="http://acme/1", pid="j1", **overrides):
    """A Job Postings row exactly as _query_all_job_postings() returns it,
    defaulted to agree with job() below so a test only has to state the one
    field it wants to differ."""
    base = {
        "page_id": pid, "url": url, "still_open": True, "title": "Product Manager",
        "company_page_id": "pg-acme", "company": ACME,
        "score": 7, "routing": "Scored", "reasoning": "Good fit",
        "ambiguity_note": None, "ic_role": True, "location": "Boston, MA",
        "ats": "Greenhouse", "new_this_run": False, "last_seen": PREVIOUS_RUN,
    }
    base.update(overrides)
    return base


def job(url="http://acme/1", **overrides):
    """A scored job as score_all() hands it to the sync."""
    base = {
        "url": url, "company": ACME, "title": "Product Manager",
        "score": 7, "routing": "Scored", "reasoning": "Good fit",
        "ambiguity_note": "", "ic_role_flag": True, "location": "Boston, MA",
        "source_ats": "Greenhouse",
    }
    base.update(overrides)
    return base


def sync(existing_rows, jobs, scraped_ok=frozenset({ACME}), dry_run=False):
    """Run sync_jobs_to_notion against stubbed Notion queries.

    Returns (result, writes, creates) where writes is a list of
    (page_id, properties) for every PATCH that actually went out. A row the
    diff decided not to touch contributes nothing to that list, which is
    what nearly every assertion here is really checking.
    """
    writes, creates = [], []

    def fake_update(page_id, props, icon_url=None, dry_run=False):
        if dry_run:
            return None       # mirrors the real helper: returns before the network
        writes.append((page_id, props))

    def fake_create(props, icon_url=None, dry_run=False):
        if dry_run:
            return None
        creates.append(props)

    nc._query_all_target_list_pages = lambda: list(TARGETS)
    nc._query_all_job_postings = lambda lookup=None: [dict(r) for r in existing_rows]
    nc._update_job_posting = fake_update
    nc._create_job_posting = fake_create
    nc.state = _StubState()

    result = nc.sync_jobs_to_notion(jobs, scraped_ok, dry_run=dry_run,
                                    rubric_fingerprint="fp-old")
    return result, writes, creates


# --- the core saving --------------------------------------------------

def test_unchanged_job_writes_nothing():
    """The whole point. A posting that came back identical costs zero
    writes, where the old code spent one PATCH of twelve properties."""
    res, writes, creates = sync([jrow()], [job()])
    assert writes == [], "an unchanged row must not be written at all"
    assert creates == []
    assert res["unchanged"] == 1
    assert res["updated"] == 0


def test_changed_score_writes_only_what_moved():
    """A real change still goes out — and carries only the fields that
    actually differ, not the whole row."""
    res, writes, _ = sync([jrow(score=7, reasoning="Good fit")],
                          [job(score=9, reasoning="Great fit")])
    assert res["updated"] == 1
    (page_id, props), = writes
    assert page_id == "j1"
    assert props == {"Score": 9, "Reasoning": "Great fit"}


def test_last_seen_never_written_for_an_open_job():
    """Cut #1. A job that came back today is open today and Still Open
    already says so; re-stamping Last Seen was a full-table write pass
    buying a value no view sorts or filters on."""
    _, writes, _ = sync([jrow(score=7)], [job(score=9)])
    (_, props), = writes
    assert "Last Seen" not in str(props)
    assert not any(k.startswith("date:") for k in props)


# --- New This Run -----------------------------------------------------

def test_new_this_run_cleared_only_where_it_is_set():
    """Cut #2. Two rows, one flagged from last run and one not. Only the
    flagged row may be written; the old code wrote __NO__ to both."""
    rows = [jrow(url="http://acme/1", pid="j1", new_this_run=True),
            jrow(url="http://acme/2", pid="j2", new_this_run=False)]
    jobs = [job(url="http://acme/1"), job(url="http://acme/2")]
    res, writes, _ = sync(rows, jobs)
    assert len(writes) == 1, "only the row that was flagged last run may be written"
    page_id, props = writes[0]
    assert page_id == "j1"
    assert props == {"New This Run": "__NO__"}
    assert res["unchanged"] == 1


def test_new_url_is_created_and_flagged_new():
    res, writes, creates = sync([], [job(url="http://acme/new")])
    assert res["created"] == 1
    assert writes == []
    props, = creates
    assert props["New This Run"] == "__YES__"
    assert props["Still Open"] == "__YES__"
    assert props["date:First Seen:start"] == TODAY
    assert props["date:Last Seen:start"] == TODAY, \
        "Last Seen is written once, at creation"
    assert props["userDefined:URL"] == "http://acme/new"
    assert props["Company"] == ["pg-acme"]


# --- close pass -------------------------------------------------------

def test_close_stamps_last_seen_with_the_previous_run():
    """The run that closes a posting is the run that did NOT see it, so
    today would record the opposite of what Last Seen means. The previous
    run is the last one that did see it."""
    rows = [jrow(url="http://acme/1", pid="j1"),
            jrow(url="http://acme/gone", pid="j2")]
    rows += [jrow(url="http://acme/pad%d" % i, pid="p%d" % i) for i in range(30)]
    jobs = [job(url="http://acme/1")]
    jobs += [job(url="http://acme/pad%d" % i) for i in range(30)]

    res, writes, _ = sync(rows, jobs)
    assert res["closed"] == 1
    closing = [(pid, p) for pid, p in writes if p.get("Still Open") == "__NO__"]
    (pid, props), = closing
    assert pid == "j2"
    assert props["date:Last Seen:start"] == PREVIOUS_RUN
    assert props["date:Last Seen:start"] != TODAY


def test_closing_row_not_flagged_new_carries_no_checkbox():
    """New This Run is reset on close only when it is actually set, so a
    closing row that was never flagged doesn't carry a redundant property."""
    rows = [jrow(url="http://acme/gone", pid="j2", new_this_run=False)]
    rows += [jrow(url="http://acme/pad%d" % i, pid="p%d" % i) for i in range(30)]
    jobs = [job(url="http://acme/pad%d" % i) for i in range(30)]
    _, writes, _ = sync(rows, jobs)
    closing = [p for pid, p in writes if pid == "j2"]
    assert closing and "New This Run" not in closing[0]


def test_reopened_job_gets_still_open_back():
    """A closed row whose URL turns up again has to come back, or it stays
    invisible to the Scored List view, which filters on Still Open."""
    res, writes, _ = sync([jrow(still_open=False)], [job()])
    assert res["updated"] == 1
    (_, props), = writes
    assert props == {"Still Open": "__YES__"}


# --- comparisons that must not drift ----------------------------------

def test_empty_note_does_not_look_changed():
    """Notion returns None for an empty rich_text; the scraper returns "".
    Comparing those naively rewrites every job with no Ambiguity Note on
    every run."""
    _, writes, _ = sync([jrow(ambiguity_note=None)], [job(ambiguity_note="")])
    assert writes == []


def test_integer_score_matches_notion_float():
    """Notion hands back a number, so 7 written comes back 7.0. Under ==
    that is a change, and every scored row rewrites forever."""
    _, writes, _ = sync([jrow(score=7.0)], [job(score=7)])
    assert writes == []


def test_overlong_reasoning_does_not_rewrite_forever():
    """_build_job_properties() truncates at 2000 chars, so a longer value
    can never read back equal to what was handed in. The comparison has to
    truncate the same way or the row is rewritten on every run."""
    long_reason = "x" * 2500
    _, writes, _ = sync([jrow(reasoning=long_reason[:2000])],
                        [job(reasoning=long_reason)])
    assert writes == []


def test_routing_select_matches_notions_own_casing():
    """The one that actually escaped into production. Notion matches a
    select name case-insensitively and returns its OWN casing: the
    pipeline emits "Needs review", the option is named "Needs Review", so
    the write was accepted, stored as "Needs Review", and read back
    differing from what was asked for. Under a naive == that rewrote 745
    rows every run forever while the sync reported itself healthy."""
    _, writes, _ = sync([jrow(routing="Needs Review")],
                        [job(routing="Needs review")])
    assert writes == [], "a select differing only in case is not a change"


def test_routing_change_that_is_real_still_writes():
    """The case-insensitive compare must not swallow an actual reroute."""
    _, writes, _ = sync([jrow(routing="Scored")], [job(routing="Non-fit")])
    (_, props), = writes
    assert props == {"Routing": "Non-fit"}


def test_missing_company_relation_is_not_cleared():
    """A company absent from the Target List under this name must leave the
    existing relation alone. Blanking it would put the row permanently
    beyond the close pass, which refuses to touch unresolved rows."""
    _, writes, _ = sync([jrow()], [job(company="Not In Target List")])
    assert all("Company" not in props for _, props in writes)


def test_failed_scoring_does_not_blank_a_good_score():
    """A job whose scoring failed comes back with score and routing None.
    None means "no answer this run", not "clear the field" — writing it
    would blank a good score, and since _build_job_properties() drops None
    the PATCH would go out carrying nothing at all."""
    _, writes, _ = sync([jrow(score=8, routing="Scored")],
                        [job(score=None, routing=None)])
    assert writes == [], "a failed score must not write, let alone blank the row"


# --- dry run ----------------------------------------------------------

def test_dry_run_writes_nothing_but_still_counts():
    res, writes, creates = sync(
        [jrow(url="http://acme/1", pid="j1", score=7)],
        [job(url="http://acme/1", score=9), job(url="http://acme/new")],
        dry_run=True)
    assert writes == [] and creates == []
    assert res["updated"] == 1 and res["created"] == 1


def test_dry_run_does_not_advance_saved_state():
    """Same reasoning as mark_new_postings(): a rehearsal that persists
    state consumes the signal it was meant to preview. Advance last_run in
    a dry run and the next real run stamps closing postings with the date
    of a run that never happened."""
    sync([jrow()], [job()], dry_run=True)
    assert nc.state.saved == []

    sync([jrow()], [job()], dry_run=False)
    assert nc.state.saved, "a real run must record its fingerprint and date"


if __name__ == "__main__":
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

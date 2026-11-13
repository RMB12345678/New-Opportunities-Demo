"""The Job Postings read must survive Notion's per-query row limit (invariant 26).

Notion ends a single query at 10,000 rows with has_more=false and no error.
The fake below reproduces that with a small cap, so these tests fail the way
production failed: the read returns a short list and nobody is told.
"""
import datetime

import pytest

import notion.client as nc

UTC = datetime.timezone.utc

# Bound at import, which pytest does for every test module before running any
# test: test_close_pass.py and test_delta_sync.py assign a stub over
# nc._query_all_job_postings and never put it back, so by the time these tests
# run the module attribute may no longer be the real function.
REAL_QUERY_ALL = nc._query_all_job_postings


class FakeResp:
    ok = True
    status_code = 200

    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class FakeNotion:
    """Applies created_time filters and sorts, pages 100 at a time, and cuts
    every query off at `cap` results exactly like the real API does."""

    def __init__(self, created_times, cap):
        self.cap = cap
        self.pages = [{
            "id": f"pg-{i}", "created_time": t.isoformat().replace("+00:00", "Z"),
            "properties": {"Still Open": {"checkbox": True},
                           "URL": {"url": f"http://x/{i}"}},
        } for i, t in enumerate(created_times)]
        self.queries = 0

    @staticmethod
    def _t(page):
        return datetime.datetime.fromisoformat(page["created_time"].replace("Z", "+00:00"))

    def _matches(self, page, flt):
        if not flt:
            return True
        conds = flt["and"]
        t = self._t(page)
        for c in conds:
            cond = c["created_time"]
            if "on_or_after" in cond and not t >= datetime.datetime.fromisoformat(cond["on_or_after"]):
                return False
            if "before" in cond and not t < datetime.datetime.fromisoformat(cond["before"]):
                return False
        return True

    def post(self, url, headers=None, json=None, timeout=None):
        self.queries += 1
        pages = [p for p in self.pages if self._matches(p, json.get("filter"))]
        if json.get("sorts"):
            pages.sort(key=self._t)
        pages = pages[:self.cap]                      # the silent cutoff
        start = int(json.get("start_cursor") or 0)
        size = json.get("page_size", 100)
        chunk = pages[start:start + size]
        more = start + size < len(pages)
        return FakeResp({"results": chunk, "has_more": more,
                         "next_cursor": str(start + size) if more else None})


def install(monkeypatch, created_times, cap):
    fake = FakeNotion(created_times, cap)
    monkeypatch.setattr(nc.session, "post", fake.post)
    monkeypatch.setattr(nc, "JOB_QUERY_CAP", cap)
    nc.reset_api_calls()
    return fake


def spread(year, month, n, day_step=1):
    return [datetime.datetime(year, month, 1 + (i * day_step) % 27, i % 24, 0, tzinfo=UTC)
            for i in range(n)]


def test_fake_really_truncates_an_unsliced_query(monkeypatch):
    """Guard on the test double itself: if this stopped truncating, every
    other test here would pass for the wrong reason."""
    install(monkeypatch, spread(2026, 8, 40) + spread(2026, 9, 40), cap=50)
    assert len(nc._fetch_job_postings(None, None)) == 50


def test_read_returns_every_row_across_months(monkeypatch):
    times = spread(2026, 7, 40) + spread(2026, 8, 40) + spread(2026, 9, 40)
    install(monkeypatch, times, cap=50)
    rows = REAL_QUERY_ALL()
    assert len(rows) == 120
    assert len({r["page_id"] for r in rows}) == 120


def test_a_month_at_the_cap_is_split_not_believed(monkeypatch):
    install(monkeypatch, spread(2026, 9, 120), cap=50)   # one month, 120 rows
    rows = REAL_QUERY_ALL()
    assert len(rows) == 120
    assert len({r["page_id"] for r in rows}) == 120


def test_unsplittable_window_raises_instead_of_returning_short(monkeypatch):
    same_minute = [datetime.datetime(2026, 9, 5, 12, 0, tzinfo=UTC)] * 60
    install(monkeypatch, same_minute, cap=50)
    with pytest.raises(RuntimeError, match="cannot be split further"):
        REAL_QUERY_ALL()


def test_empty_database_reads_as_empty(monkeypatch):
    install(monkeypatch, [], cap=50)
    assert REAL_QUERY_ALL() == []


def test_windows_are_contiguous_and_cover_through_tomorrow():
    now = datetime.datetime(2026, 10, 29, 15, 0, tzinfo=UTC)
    wins = nc._job_posting_windows(datetime.datetime(2026, 8, 21, 10, 0, tzinfo=UTC), now)
    assert wins[0][0] == datetime.datetime(2026, 8, 1, tzinfo=UTC)
    assert all(a[1] == b[0] for a, b in zip(wins, wins[1:]))
    assert wins[-1][1] > now + datetime.timedelta(days=1)
    assert datetime.datetime(2026, 12, 31, tzinfo=UTC).month == 12 and \
        nc._job_posting_windows(datetime.datetime(2026, 12, 15, tzinfo=UTC),
                                datetime.datetime(2027, 1, 5, tzinfo=UTC))[0][1] == \
        datetime.datetime(2027, 1, 1, tzinfo=UTC)


# --- archive selection ------------------------------------------------------

TODAY = datetime.date(2027, 3, 1)           # cutoff = 2026-08-30


def arow(**kw):
    base = {"page_id": "p", "url": "http://x/1", "still_open": False,
            "first_seen": "2026-01-10", "application_status": None, "date_applied": None}
    base.update(kw)
    return base


def test_old_closed_row_is_archived():
    take, kept = nc.select_archivable_postings([arow()], TODAY)
    assert len(take) == 1 and not kept


@pytest.mark.parametrize("override,reason", [
    ({"still_open": True}, "still open"),
    ({"url": None}, "no URL (added by hand)"),
    ({"application_status": "Applied"}, "has application history"),
    ({"date_applied": "2026-02-02"}, "has application history"),
    ({"first_seen": None}, "First Seen blank (age unknown)"),
    ({"first_seen": "2026-11-01"}, "closed but newer than 6 months"),
])
def test_rows_that_must_survive(override, reason):
    take, kept = nc.select_archivable_postings([arow(**override)], TODAY)
    assert take == []
    assert kept[reason] == 1


def test_age_boundary_is_exclusive_of_the_cutoff_day():
    cutoff = (TODAY - datetime.timedelta(days=nc.ARCHIVE_AFTER_DAYS)).isoformat()
    take, _ = nc.select_archivable_postings([arow(first_seen=cutoff)], TODAY)
    assert take == []


def test_first_seen_with_a_time_component_still_compares_on_the_date():
    take, _ = nc.select_archivable_postings([arow(first_seen="2026-01-10T08:30:00.000+00:00")], TODAY)
    assert len(take) == 1


def test_archive_under_dry_run_touches_no_network_but_is_counted(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("dry run reached the network")
    monkeypatch.setattr(nc.session, "patch", boom)
    nc.reset_api_calls()
    assert nc._archive_job_posting("pg-1", dry_run=True) is None
    assert nc.api_call_report()["writes"] == {"write:job_archive": 1}

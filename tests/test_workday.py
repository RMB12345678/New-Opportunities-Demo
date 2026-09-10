"""Tests for the Workday scraper.

Workday companies used to fall through to the generic HTML scraper and
report zero open roles while actually having hundreds, so the things worth
protecting here are the ones that made that failure hard to see:

  - URL parsing has to survive the two shapes that already bit us: a
    multi-digit datacenter number (Acumed is on wd501, and a `wd\\d`
    pattern truncates that to wd5 — a different, dead host that answers
    every request with a 500), and the optional /en-US/ locale segment.
  - Paging has to keep going past the first 20. Workday's page size is
    capped at 20 by the server, so a board of 1,120 jobs that returns 20
    looks exactly like a small company unless the loop is right.
  - A shared tenant's facet filter must never silently degrade into the
    unfiltered board. Halma's tenant hosts 41 companies; returning all of
    them as Microsurgical Technology's postings would be wrong in a way
    nothing downstream could detect.

Every HTTP call is stubbed, so the suite makes no network calls, spends
nothing, and needs no credentials.

Runs under pytest, and standalone (`python tests/test_workday.py`) for the
case where pytest isn't installed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scrapers import workday


# --- URL parsing -----------------------------------------------------------

def test_parses_a_plain_board_url():
    p = workday.parse_board_url("https://transmedics.wd1.myworkdayjobs.com/TransMedics_Careers")
    assert p["host"] == "transmedics"
    assert p["wd"] == "wd1"
    assert p["tenant"] == "transmedics"
    assert p["site"] == "TransMedics_Careers"
    assert p["applied_facets"] == {}


def test_multi_digit_datacenter_survives():
    """Acumed's board is on wd501. Truncating that to wd5 hits a dead host
    that 500s, which is the original 'too many 500 error responses' bug."""
    p = workday.parse_board_url("https://marmon.wd501.myworkdayjobs.com/Marmon_Careers")
    assert p["wd"] == "wd501"


def test_locale_segment_is_not_mistaken_for_the_site():
    p = workday.parse_board_url("https://transmedics.wd1.myworkdayjobs.com/en-US/TransMedics_Careers")
    assert p["site"] == "TransMedics_Careers"


def test_tenant_converts_hyphens_to_underscores():
    """United Therapeutics is hosted at vhr-unither but its tenant is
    vhr_unither. The hyphen form returns 422 for every site name, which
    makes a bad tenant look like a bad site."""
    p = workday.parse_board_url("https://vhr-unither.wd5.myworkdayjobs.com/External")
    assert p["host"] == "vhr-unither"
    assert p["tenant"] == "vhr_unither"


def test_facet_filter_is_carried_through_with_its_real_key():
    """The key is not always 'hiringCompany' — Marmon uses a custom field —
    so it is read from the URL rather than hardcoded."""
    p = workday.parse_board_url(
        "https://marmon.wd501.myworkdayjobs.com/Marmon_Careers"
        "?CF-EE-External_Company_Customer_Org_Job_Posting_Anchor_Extended=c332f619ca3d015cc7478df1ef018b43")
    assert p["applied_facets"] == {
        "CF-EE-External_Company_Customer_Org_Job_Posting_Anchor_Extended":
            ["c332f619ca3d015cc7478df1ef018b43"]}


def test_non_facet_query_params_are_ignored():
    """Only values shaped like a Workday id count, so tracking junk doesn't
    become a filter that 400s the whole board."""
    p = workday.parse_board_url(
        "https://halma.wd3.myworkdayjobs.com/Halma?utm_source=linkedin&q=engineer")
    assert p["applied_facets"] == {}


def test_non_workday_url_returns_none():
    """A marketing careers page can't yield board parameters. None is the
    signal to flag for a human, not to guess."""
    assert workday.parse_board_url("https://microsurgical.com/careers/") is None
    assert workday.parse_board_url("https://careers.stryker.com/") is None
    assert workday.parse_board_url(None) is None


# --- fetching --------------------------------------------------------------

def fake_board(total, facets=None):
    """Stand in for a Workday board, paging 20 at a time like the real one.

    Records every payload it is asked for so tests can assert on offsets
    and on which facets were actually sent.
    """
    calls = []

    def post(api_url, payload):
        calls.append(payload)
        offset = payload["offset"]
        limit = payload["limit"]
        page = [
            {"title": "Job %d" % i,
             "locationsText": "Redmond, WA",
             "externalPath": "/job/Redmond/Job-%d_R-%d" % (i, i),
             "postedOn": "Posted 4 Days Ago"}
            for i in range(offset, min(offset + limit, total))
        ]
        return {"total": total, "jobPostings": page, "facets": facets or []}

    return post, calls


def run_fetch(total, facets=None, **kwargs):
    post, calls = fake_board(total, facets)
    original = workday._post
    workday._post = post
    try:
        jobs = workday.fetch_jobs(
            kwargs.pop("company_name", "Acme Medical"),
            host=kwargs.pop("host", "acme"), wd=kwargs.pop("wd", "wd1"),
            site=kwargs.pop("site", "Careers"), **kwargs)
    finally:
        workday._post = original
    return jobs, calls


def test_pages_past_the_first_twenty():
    """The server caps a page at 20, so a 47-job board needs three calls."""
    jobs, calls = run_fetch(47)
    assert len(jobs) == 47
    assert [c["offset"] for c in calls] == [0, 20, 40]


def test_single_page_board_makes_one_call():
    jobs, calls = run_fetch(12)
    assert len(jobs) == 12
    assert len(calls) == 1, "no pointless extra request once the board is exhausted"


def test_empty_board_is_not_an_error():
    """Zero jobs is a real answer — the company was reached and has nothing
    open. Only an exception means we never got an answer at all."""
    jobs, _ = run_fetch(0)
    assert jobs == []


def test_job_shape_matches_the_other_scrapers():
    jobs, _ = run_fetch(1)
    job = jobs[0]
    assert set(job) == {"company", "title", "location", "url", "posted_at",
                        "description", "source_ats"}
    assert job["company"] == "Acme Medical"
    assert job["source_ats"] == "Workday"
    assert job["url"] == ("https://acme.wd1.myworkdayjobs.com/en-US/Careers"
                          "/job/Redmond/Job-0_R-0")


def test_requires_real_parameters():
    """No guess-from-the-company-name fallback: a guessed Workday tenant can
    resolve to a real board belonging to somebody else."""
    try:
        workday.fetch_jobs("Acme Medical")
    except ValueError:
        pass
    else:
        raise AssertionError("missing board parameters must raise, not guess")


# --- shared tenants --------------------------------------------------------

COMPANY_FACET = [{
    "facetParameter": "hiringCompany",
    "descriptor": "Company",
    "values": [{"id": "75705bdd576d1001081740354dfc0001",
                "descriptor": "Microsurgical Technology, Inc.", "count": 15}],
}]


def test_valid_facet_is_sent_on_every_page():
    jobs, calls = run_fetch(
        25, facets=COMPANY_FACET,
        applied_facets={"hiringCompany": ["75705bdd576d1001081740354dfc0001"]})
    assert len(jobs) == 25
    # First call probes the board unfiltered to read its facet list; every
    # call after that must carry the filter.
    assert calls[0]["appliedFacets"] == {}
    assert all(c["appliedFacets"] == {"hiringCompany": ["75705bdd576d1001081740354dfc0001"]}
               for c in calls[1:])


def test_unknown_facet_key_raises_rather_than_returning_the_whole_tenant():
    """Halma's tenant hosts 41 companies. Falling back to the unfiltered
    board would attribute all of them to one subsidiary, and nothing
    downstream would ever catch it."""
    try:
        run_fetch(156, facets=COMPANY_FACET,
                  applied_facets={"notARealFacet": ["75705bdd576d1001081740354dfc0001"]})
    except ValueError as e:
        assert "hiringCompany" in str(e), "the message should list the valid facets"
    else:
        raise AssertionError("an unknown facet key must raise")


def test_dead_facet_value_raises():
    """A renamed or removed subsidiary must not silently become the whole
    tenant's job list."""
    try:
        run_fetch(156, facets=COMPANY_FACET,
                  applied_facets={"hiringCompany": ["deadbeefdeadbeefdeadbeefdeadbeef"]})
    except ValueError as e:
        assert "deadbeef" in str(e)
    else:
        raise AssertionError("an unknown facet value must raise")


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

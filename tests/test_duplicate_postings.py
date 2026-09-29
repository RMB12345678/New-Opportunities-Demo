"""Tests for the run-wide URL dedupe.

The bug: a posting's URL is its identity everywhere downstream —
sync_jobs_to_notion() keys its existing-row lookup on it, and
mark_new_postings() keys its seen-list on it. So two jobs sharing a URL
never became two postings; they became a second Notion row that nothing
could ever visit again, because the sync holds one row per URL. Those
orphans kept New This Run and Still Open ticked forever, which is how 47
rows came to be sitting in the New This Run view on 2026-09-29 — 40 of
them duplicates, the oldest from 2026-09-15.

The repeats were real, and came from two places: Workable's widget API
lists a multi-location posting once per city (collapsed at source now —
see tests/test_new_scrapers.py), and an HTML careers page can link the
same URL under several job titles. This pass is what catches the next
platform that does either.

No network, no credentials, no spend.

Runs under pytest, and standalone (`python tests/test_duplicate_postings.py`).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run


def job(url, **overrides):
    base = {"company": "Acme", "title": "Product Manager", "url": url,
            "location": "Boston, MA", "source_ats": "Workable"}
    base.update(overrides)
    return base


def test_repeated_url_keeps_the_first_copy():
    jobs = [job("http://acme/1"), job("http://acme/1", location="Paris"),
            job("http://acme/2")]
    kept = run.dedupe_jobs_by_url(jobs)
    assert [j["url"] for j in kept] == ["http://acme/1", "http://acme/2"]
    assert kept[0]["location"] == "Boston, MA", "first copy wins, consistently"


def test_distinct_urls_all_survive():
    jobs = [job("http://acme/%d" % i) for i in range(5)]
    assert run.dedupe_jobs_by_url(jobs) == jobs


def test_jobs_with_no_url_are_left_alone():
    """Two URL-less postings are not the same posting — a missing URL is
    not an identity. They cannot collide with anything, and the sync skips
    them before they can do harm."""
    jobs = [job(None), job(None, title="Regulatory Lead"), job("")]
    assert len(run.dedupe_jobs_by_url(jobs)) == 3


def test_different_titles_on_one_url_still_collapse():
    """Peritas AI lists four distinct roles that all link to /careers.
    Collapsing them loses information, deliberately: with no per-role URL
    there is nothing to tell them apart downstream, so the sync could
    never have held them as four rows either — it would have made three
    unreachable ones."""
    jobs = [job("http://peritas/careers", title=t)
            for t in ("IT Specialist", "Research Scientist", "Product Manager")]
    kept = run.dedupe_jobs_by_url(jobs)
    assert len(kept) == 1
    assert kept[0]["title"] == "IT Specialist"


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

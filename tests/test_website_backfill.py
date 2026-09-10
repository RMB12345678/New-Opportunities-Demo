"""Tests for the Website + page-icon backfill (`fill_missing_website()` in
run.py, `ats_finder/find_website.py`, and the two new notion/client.py
helpers it calls).

Website is a Notion URL-type property, so unlike ATS Platform it can't hold
a text "Not found" marker to remember a company that was genuinely checked
and came up empty. The page icon carries that memory instead — every
company this pass looks at gets one stamped, a real favicon or the blank
white square, so a company is never re-searched (and re-paid for) on every
future run once it has been looked at once. These tests protect that
contract along with the free-first derivation order.

Like the other suites here, everything that would touch the network is
stubbed, so this makes no real HTTP calls, spends nothing, and needs no
credentials. Runs under pytest, and standalone
(`python tests/test_website_backfill.py`) for when pytest isn't installed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("NOTION_API_KEY", "test-key-not-real")
os.environ.setdefault("NOTION_DATABASE_ID", "test-target-list-db")
os.environ.setdefault("JOB_POSTINGS_DATABASE_ID", "test-job-postings-db")

import notion.client as nc
from ats_finder import find_website as fw


# --- _real_company_host() / favicon_icon_url() -------------------------

def test_a_real_company_domain_is_used_as_is():
    assert fw._real_company_host("https://www.acmemedical.com/careers") == "acmemedical.com"


def test_known_ats_hosts_are_never_treated_as_the_company():
    assert fw._real_company_host("https://job-boards.greenhouse.io/neuralink") is None
    assert fw._real_company_host("https://jobs.lever.co/acme") is None
    assert fw._real_company_host("https://jobs.ashbyhq.com/bivacor-inc") is None
    assert fw._real_company_host("https://acme.myworkdayjobs.com/careers") is None


def test_google_search_fallback_url_is_not_a_company_host():
    """fill_missing_careers_urls()'s own last-resort fallback (a pre-filled
    Google search) must not be mistaken for the company's real domain."""
    assert fw._real_company_host("https://www.google.com/search?q=acme+careers") is None


def test_hostless_or_malformed_urls_return_none():
    assert fw._real_company_host(None) is None
    assert fw._real_company_host("http://phoenixkinetics") is None  # no dot — not a real host
    assert fw._real_company_host("some descriptive sentence, not a url") is None


def test_www_prefix_is_stripped():
    assert fw._real_company_host("https://www.acme.com") == "acme.com"


def test_favicon_url_is_googles_public_endpoint():
    url = fw.favicon_icon_url("acme.com")
    assert url == "https://www.google.com/s2/favicons?domain=acme.com&sz=128"


# --- derive_website() ---------------------------------------------------

def test_a_real_careers_url_resolves_for_free_with_no_search_call():
    """The whole point of trying the Careers URL host first: this must not
    call find_website_homepage() (i.e. not spend an Anthropic call) when
    the Careers URL already IS the company's own domain."""
    called = []
    original = fw.find_website_homepage
    fw.find_website_homepage = lambda *a, **k: called.append(1) or "should not be used"
    try:
        website, icon = fw.derive_website("https://acme.com/careers", "Acme Medical")
    finally:
        fw.find_website_homepage = original

    assert website == "https://acme.com"
    assert icon == "https://www.google.com/s2/favicons?domain=acme.com&sz=128"
    assert called == [], "a real company host must not trigger the paid search fallback"


def test_ats_hosted_careers_url_falls_back_to_search():
    fw.find_website_homepage = lambda *a, **k: "https://www.realcompany.com"
    try:
        website, icon = fw.derive_website(
            "https://job-boards.greenhouse.io/realcompany", "Real Company")
    finally:
        del fw.find_website_homepage

    assert website == "https://realcompany.com"
    assert icon == "https://www.google.com/s2/favicons?domain=realcompany.com&sz=128"


def test_genuinely_unresolvable_company_gets_the_blank_icon_not_none():
    """A company nothing can be found for still gets a result — the blank
    marker — not just (None, None). Returning (None, None) here is exactly
    the bug that would make this company get re-searched every future run."""
    fw.find_website_homepage = lambda *a, **k: None
    try:
        website, icon = fw.derive_website(None, "Totally Obscure Startup")
    finally:
        del fw.find_website_homepage

    assert website is None
    assert icon == fw.BLANK_ICON == "⬜"


def test_a_failed_search_raises_instead_of_returning_a_blank():
    """A failed LOOKUP (network/API outage) must propagate as an exception,
    not come back looking like a genuine 'nothing found' — otherwise a
    transient outage would permanently blank-stamp every company hit during
    it, exactly the invariant-1 trap fill_missing_ats() already guards
    against."""
    def exploding(*a, **k):
        raise RuntimeError("Anthropic API is down")
    fw.find_website_homepage = exploding
    try:
        raised = False
        try:
            fw.derive_website(None, "Some Company")
        except RuntimeError:
            raised = True
        assert raised, "a failed lookup must raise, not return (None, BLANK_ICON)"
    finally:
        del fw.find_website_homepage


# --- get_companies_missing_website() ------------------------------------

def test_missing_website_filter_needs_both_no_website_and_no_icon():
    rows = [
        {"website": None, "has_icon": False},   # never processed -> missing
        {"website": "https://acme.com", "has_icon": False},  # has a value -> not missing
        {"website": None, "has_icon": True},     # already stamped (blank or real) -> not missing
        {"website": "https://x.com", "has_icon": True},      # fully done -> not missing
    ]
    original = nc.get_all_companies
    nc.get_all_companies = lambda: rows
    try:
        missing = nc.get_companies_missing_website()
    finally:
        nc.get_all_companies = original

    assert missing == [rows[0]]


# --- update_company_website() -------------------------------------------

def _stub_patch(capture):
    class FakeResp:
        ok = True
        def json(self):
            return {"id": "pg-1"}
    def fake_patch(url, headers=None, json=None):
        capture.append({"url": url, "json": json})
        return FakeResp()
    return fake_patch


def test_writes_a_real_favicon_as_an_external_icon():
    captured = []
    original = nc.session.patch
    nc.session.patch = _stub_patch(captured)
    try:
        nc.update_company_website(
            "pg-1", website="https://acme.com",
            icon="https://www.google.com/s2/favicons?domain=acme.com&sz=128")
    finally:
        nc.session.patch = original

    payload = captured[0]["json"]
    assert payload["properties"]["Website"] == {"url": "https://acme.com"}
    assert payload["icon"] == {
        "type": "external",
        "external": {"url": "https://www.google.com/s2/favicons?domain=acme.com&sz=128"},
    }


def test_writes_the_blank_marker_as_an_emoji_icon():
    captured = []
    original = nc.session.patch
    nc.session.patch = _stub_patch(captured)
    try:
        nc.update_company_website("pg-2", website=None, icon=fw.BLANK_ICON)
    finally:
        nc.session.patch = original

    payload = captured[0]["json"]
    assert "properties" not in payload, "website=None must not touch the Website property"
    assert payload["icon"] == {"type": "emoji", "emoji": "⬜"}


def test_dry_run_writes_nothing():
    original = nc.session.patch
    def exploding(*a, **k):
        raise AssertionError("dry_run must not hit the network")
    nc.session.patch = exploding
    try:
        result = nc.update_company_website(
            "pg-3", website="https://acme.com", icon="https://example.com/f.png", dry_run=True)
    finally:
        nc.session.patch = original
    assert result is None


def test_nothing_to_write_is_a_no_op():
    original = nc.session.patch
    def exploding(*a, **k):
        raise AssertionError("must not hit the network when there is nothing to write")
    nc.session.patch = exploding
    try:
        result = nc.update_company_website("pg-4")
    finally:
        nc.session.patch = original
    assert result is None


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

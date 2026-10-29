"""Tests for treating a *wrong* saved value as unresearched.

The pipeline's "have we looked at this company" test was "is the field
blank" (invariant 8). That is a good test for a field whose wrong answers
are still answers, and no test at all for one holding something that was
never an answer. Twenty rows carried the same pasted `app.notion.com` link
in `Careers URL`, and the consequences compounded:

  - `get_companies_missing_url()` selected on a blank field, so the one
    pass that could have repaired them skipped them on every run, while
    the scrape retried and failed on every run. A permanent loop.
  - `derive_website()` reads the Careers URL, so eighteen of them had
    `Website` set to https://app.notion.com and their page icon stamped
    with Notion's favicon — which invariant 16 reads as "already checked",
    making the bad value permanent too.

The root cause of the second was a one-line omission: NON_COMPANY_HOSTS
listed notion.so and notion.site but not notion.com.

What these protect is the narrowness of the fix. The predicate must reject
what is structurally impossible and nothing else — a value a human chose
deliberately has to keep passing, or acting on it (which means overwriting
it) would destroy real answers. The Google-search fallback is the case
that matters: fill_missing_careers_urls() writes one deliberately as its
documented last resort, and rejecting it would re-open the exact
re-research-forever loop invariant 8 exists to close.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ats_finder.careers_url import is_usable_careers_url
from ats_finder.find_website import (
    NON_COMPANY_HOSTS, _real_company_host, is_company_domain,
)


# --- what must be rejected -------------------------------------------------

def test_the_pasted_notion_link_is_rejected():
    """The actual value that was in twenty rows."""
    assert not is_usable_careers_url(
        "https://app.notion.com/p/PocDoc-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        "?v=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx&source=copy_link")


def test_document_hosts_are_rejected_including_subdomains():
    for url in (
        "https://notion.so/some-page",
        "https://www.notion.site/careers",
        "https://docs.google.com/document/d/abc/edit",
        "https://drive.google.com/file/d/abc/view",
        "https://dropbox.com/s/abc/jobs.pdf",
        "https://company.box.com/s/abc",
        "https://acme.sharepoint.com/sites/hr",
        "https://airtable.com/shrABC",
    ):
        assert not is_usable_careers_url(url), url


def test_structurally_broken_values_are_rejected():
    assert not is_usable_careers_url(None)
    assert not is_usable_careers_url("")
    assert not is_usable_careers_url("   ")
    assert not is_usable_careers_url("not a url at all")
    assert not is_usable_careers_url("careers.acme.com/jobs"), "no scheme"
    assert not is_usable_careers_url("ftp://acme.com/jobs"), "not http(s)"
    assert not is_usable_careers_url("https://localhost/jobs"), "no dot in host"
    assert not is_usable_careers_url(
        '"Search Jobs - DIH Site Careers - oracle" hosted at eisu.fa.em2.oraclecloud.com.'), \
        "a whole sentence saved into the field"


# --- what must keep passing ------------------------------------------------

def test_the_google_search_fallback_is_accepted():
    """fill_missing_careers_urls() writes this deliberately when no careers
    page and no homepage can be found. It is a real, clickable, chosen
    answer — invariant 8 exists so those rows stop being re-researched.
    Rejecting it would re-open that loop, which is why only the docs/drive
    subdomains are listed and google.com itself is not."""
    assert is_usable_careers_url("https://www.google.com/search?q=Acme+Medical+careers")


def test_real_careers_urls_are_accepted():
    for url in (
        "https://boards.greenhouse.io/acme",
        "https://jobs.lever.co/acme",
        "https://apply.workable.com/cvrx/",
        "https://cyted.jobs.personio.com",
        "https://astorg.pinpointhq.com/",
        "https://www.acme.com/careers/",
        "http://acme.com/jobs",
        "https://acme.com:8443/careers",
        "https://careers.acme.co.uk/",
    ):
        assert is_usable_careers_url(url), url


def test_a_stale_or_dead_url_still_passes():
    """Liveness is deliberately not judged here. A URL that 404s is a
    scrape failure, which sync_scrape_status() already surfaces; calling it
    invalid would collide with invariant 1, where a fetch that fails to run
    is not evidence the URL is wrong."""
    assert is_usable_careers_url("https://brainsway.breezy.hr/"), \
        "this board is dead, but the URL is structurally fine"


# --- the Website leak ------------------------------------------------------

def test_notion_com_is_a_non_company_host():
    """The one-line omission that set eighteen companies' Website to
    https://app.notion.com: notion.so and notion.site were listed, the
    host actually in use was not."""
    assert "notion.com" in NON_COMPANY_HOSTS
    assert _real_company_host("https://app.notion.com/p/abc") is None
    assert not is_company_domain("https://app.notion.com")


def test_is_company_domain_still_accepts_real_company_sites():
    assert is_company_domain("https://www.empatica.com/")
    assert is_company_domain("https://acme.co.uk")
    assert not is_company_domain("https://boards.greenhouse.io/acme")
    assert not is_company_domain(None)


# --- the selectors ---------------------------------------------------------

def _missing_url(rows):
    """Re-implements get_companies_missing_url()'s predicate over fixtures,
    so the selection rule is tested without importing notion.client (which
    reads env vars and would need stubbing for no gain here)."""
    return [
        r for r in rows
        if r["ats_platform"]
        and "AMBIGUOUS" not in r["ats_platform"]
        and "N/A" not in r["ats_platform"]
        and "Not found" not in r["ats_platform"]
        and not is_usable_careers_url(r["careers_url"])
    ]


def test_a_bad_url_is_selected_for_repair_and_a_good_one_is_not():
    rows = [
        {"company": "Pasted", "ats_platform": "Workable",
         "careers_url": "https://app.notion.com/p/abc"},
        {"company": "Blank", "ats_platform": "Workable", "careers_url": None},
        {"company": "Real", "ats_platform": "Workable",
         "careers_url": "https://apply.workable.com/real/"},
        {"company": "Googled", "ats_platform": "None (in-house careers page)",
         "careers_url": "https://www.google.com/search?q=Googled+careers"},
    ]
    picked = {r["company"] for r in _missing_url(rows)}
    assert picked == {"Pasted", "Blank"}, \
        "the pasted link and the blank get researched; the real URL and the " \
        "deliberate Google fallback are left alone"


def test_conclusive_ats_markers_still_win_over_a_bad_url():
    """A row whose ATS Platform is a conclusive "Not found" is excluded
    regardless — re-searching just its URL is waste we already paid for.
    Widening the URL test must not quietly widen that."""
    rows = [{"company": "Firm", "ats_platform": "Not found (auto-search failed)",
             "careers_url": "https://app.notion.com/p/abc"}]
    assert _missing_url(rows) == []


def _missing_website(rows):
    """Re-implements get_companies_missing_website()'s predicate."""
    return [
        r for r in rows
        if (not r["website"] and not r["has_icon"])
        or (r["website"] and not is_company_domain(r["website"]))
    ]


def test_a_bogus_website_is_reselected_even_though_it_has_an_icon():
    """Invariant 16 makes the icon the "already checked" marker, which is
    what made https://app.notion.com permanent on eighteen rows."""
    rows = [
        {"company": "Bogus", "website": "https://app.notion.com", "has_icon": True},
        {"company": "Good", "website": "https://empatica.com", "has_icon": True},
        {"company": "Checked-empty", "website": None, "has_icon": True},
        {"company": "Never-looked", "website": None, "has_icon": False},
    ]
    picked = {r["company"] for r in _missing_website(rows)}
    assert picked == {"Bogus", "Never-looked"}, \
        "a checked-and-genuinely-empty row must NOT be reselected"


def test_the_bogus_website_clause_cannot_loop():
    """fill_missing_website() passes `website or ""`, so a genuine "nothing
    found" clears the field instead of leaving the bad value. Without that,
    a row with a bogus Website and no derivable real one would be selected,
    written, and selected again every run forever."""
    row = {"company": "Bogus", "website": "https://app.notion.com", "has_icon": True}
    assert _missing_website([row])

    # What fill_missing_website() now writes when derive_website() finds
    # nothing: website="" -> {"url": None} -> field cleared, icon stamped.
    derived_website = None
    row = {"company": "Bogus", "website": (derived_website or "") or None, "has_icon": True}
    assert _missing_website([row]) == [], "cleared + iconed must not be reselected"


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

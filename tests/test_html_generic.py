"""Tests for the generic HTML careers-page scraper.

The heuristic extractor can't tell "this page genuinely has zero openings"
from "this page isn't the company's board at all" — both look like zero
matched job-title links. run.py deliberately trusts a zero-job result as a
real SCRAPE_OK (see its docstring), which is correct for the first case and
wrong for the second. One PE firm's Careers URL sat at "https://jobs.workable.com"
(Workable's own generic domain, not that firm's board) for a week, stamped OK
with "HTML: 0 job(s)", because nothing distinguished the two cases.

_reject_bare_ats_domain narrows that gap for one concrete, checkable shape:
a saved URL that is a known multi-tenant ATS's own domain with no
company-specific path. These tests protect that check specifically, not a
general "zero jobs is suspicious" rule — that broader rule would fight the
documented, deliberate reason zero is trusted elsewhere in this pipeline.

A second, separate bug lived in the text extraction itself: get_text()
without an explicit separator joins adjacent sibling elements (title,
location, employment type — a layout Personio and others use) with no
space, so "Data Architect" next to "Barcelona" became the single run
"Data ArchitectBarcelona" and silently broke the \b word-boundary check
in JOB_TITLE_HINTS whenever the hint word was the title's last word. On
one medtech company's real Personio-hosted board (verified live) this
undercounted the open roles to 3 — not an exception, not a zero-jobs flag, just most
titles no longer looking like titles.

Fixing the separator alone still left 2 of those 9 uncaught, both flagged
by the same board: "Senior Talent Partner" matched no hint word at all
("partner" wasn't in the list), and the longest title tripped the
100-char length cap once location/employment text got appended to it.
Both are fixed below (partner added to JOB_TITLE_HINTS; MAX_TITLE_LENGTH
raised to 160) and test_adjacent_elements_dont_run_words_together now
covers the full 9, not just the 7 the separator fix alone recovered.

Every HTTP call is stubbed, so the suite makes no network calls, spends
nothing, and needs no credentials.

Runs under pytest, and standalone (`python tests/test_html_generic.py`) for
the case where pytest isn't installed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scrapers import html_generic


# --- the bare-ATS-domain guard ---------------------------------------------

def test_bare_workable_domain_is_rejected():
    """The exact real-world shape: Workable's own domain, no company slug."""
    try:
        html_generic.fetch_jobs("Acme Capital", "https://jobs.workable.com")
    except ValueError as e:
        assert "Acme Capital" in str(e)
        assert "workable.com" in str(e)
    else:
        raise AssertionError("a bare ATS domain must raise, not scrape to zero")


def test_bare_domain_with_trailing_slash_is_still_rejected():
    try:
        html_generic.fetch_jobs("Acme Capital", "https://jobs.workable.com/")
    except ValueError:
        pass
    else:
        raise AssertionError("a trailing slash alone is still not a company path")


def test_company_specific_workable_url_is_not_rejected():
    """apply.workable.com/<company>/ is a real board and must reach the
    fetch step (and therefore the stubbed session), not raise."""
    jobs = run_fetch("Acme Capital", "https://apply.workable.com/acmecapital/", html="<html></html>")
    assert jobs == []


def test_other_known_ats_root_domains_are_also_rejected():
    for url in (
        "https://boards.greenhouse.io",
        "https://jobs.lever.co",
        "https://jobs.ashbyhq.com",
        "https://www.myworkdayjobs.com",
    ):
        try:
            html_generic.fetch_jobs("Acme Medical", url)
        except ValueError:
            continue
        raise AssertionError(f"{url} should have been rejected as a bare ATS domain")


def test_companys_own_domain_is_never_rejected():
    """The check is scoped to the known multi-tenant ATS hosts. A company's
    own site, with or without a path, must reach the fetch step."""
    for url in ("https://example.com", "https://example.com/careers"):
        jobs = run_fetch("Example Co", url, html="<html></html>")
        assert jobs == []


# --- fetching (unaffected by the guard) ------------------------------------

class _FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class _FakeSession:
    def __init__(self, html):
        self.html = html
        self.requested_urls = []

    def get(self, url, headers=None, timeout=None):
        self.requested_urls.append(url)
        return _FakeResponse(self.html)


def run_fetch(company_name, careers_url, html):
    fake = _FakeSession(html)
    original = html_generic.session
    html_generic.session = fake
    try:
        return html_generic.fetch_jobs(company_name, careers_url)
    finally:
        html_generic.session = original


def test_extracts_job_shaped_links():
    html = """
    <html><body>
      <nav><a href="/careers">Careers</a></nav>
      <a href="/jobs/1">Senior Manufacturing Engineer</a>
      <a href="/jobs/2">Clinical Specialist</a>
    </body></html>
    """
    jobs = run_fetch("Acme Medical", "https://acme.com/careers", html)
    assert {j["title"] for j in jobs} == {"Senior Manufacturing Engineer", "Clinical Specialist"}
    assert all(j["company"] == "Acme Medical" for j in jobs)
    assert all(j["source_ats"] == "HTML (generic scrape)" for j in jobs)


def test_nav_noise_is_filtered_even_with_a_hint_word():
    html = '<html><body><a href="/careers">Careers</a></body></html>'
    jobs = run_fetch("Acme Medical", "https://acme.com/careers", html)
    assert jobs == []


def test_genuinely_empty_board_returns_empty_not_an_error():
    """A real company page with no matching links is still a legitimate
    zero — the guard above is about the URL being wrong, not the count
    being zero."""
    jobs = run_fetch("Acme Medical", "https://acme.com/careers", "<html><body></body></html>")
    assert jobs == []


# --- adjacent-element text joining (the run-on title bug) -----------------

def _personio_style_link(job_id, title, location="Barcelona",
                          employment="Full-time", contract="Permanent employee"):
    """One <a> with title/location/employment-type/contract-type as separate
    sibling spans and no whitespace text node between them in the source —
    the exact shape (all four fields) that collapsed into a run-on string
    under the old separator-less get_text(strip=True)."""
    return (f'<a href="/job/{job_id}">'
            f'<span>{title}</span><span>{location}</span>'
            f'<span>{employment}</span><span>{contract}</span>'
            f'</a>')


def test_adjacent_elements_dont_run_words_together():
    """Reproduces a real Personio board's shape (verified live, Sep 2026):
    every open role on it, in the exact title text it uses. Before the
    separator fix, 6 of 9 lost their hint word to the glued-on location.
    Before the hint-list and length-cap fixes, "Senior Talent Partner" and
    the longest title were still dropped even with the separator fixed.
    All 9 must be found now."""
    titles = [
        "Clinical Application and Support Engineer",
        "Clinical Operations Coordinator",
        "Data Architect",
        "Electronics Technician",
        "Senior Mechanical Engineer (with Medical Devices Experience)",
        "Process Engineer (Semiconductor Manufacturing)",
        "Senior MEMS Advanced Packaging Engineer",
        "Senior Talent Partner",
        "Industrial & UX Designer",
        "Senior Quality Engineer (Production)",
        "Machine Learning Operations Architect (with medical device experience)",
    ]
    html = "<html><body>" + "".join(
        _personio_style_link(1000 + i, t) for i, t in enumerate(titles)
    ) + "</body></html>"
    jobs = run_fetch("Acme Neurotech", "https://acme-neurotech.jobs.personio.com/?language=en", html)
    found_titles = {j["title"] for j in jobs}
    for t in titles:
        assert any(t in found for found in found_titles), f"missing: {t}"
    assert len(jobs) == len(titles), (
        f"expected all {len(titles)} roles, got {len(jobs)}: {sorted(found_titles)}")


def test_title_and_location_are_not_glued_together():
    """The specific mechanism: without a separator, 'Architect' and
    'Barcelona' become one word and the hint regex can't see 'Architect'
    as a whole word anymore."""
    html = "<html><body>" + _personio_style_link(1, "Data Architect") + "</body></html>"
    jobs = run_fetch("Acme Neurotech", "https://acme-neurotech.example.com/careers", html)
    assert len(jobs) == 1
    assert "ArchitectBarcelona" not in jobs[0]["title"]
    assert "Architect" in jobs[0]["title"]


def test_partner_is_a_recognized_title_word():
    html = "<html><body>" + _personio_style_link(1, "Senior Talent Partner") + "</body></html>"
    jobs = run_fetch("Acme Neurotech", "https://acme-neurotech.example.com/careers", html)
    assert len(jobs) == 1


def test_partners_nav_link_is_still_not_matched():
    """The reason 'partner' is safe to add: \\b requires a boundary right
    after it, and a nav link reading 'Partners' or 'Our Partnerships' has
    a word character ('s'/'sh') right there instead, so it still doesn't
    match — this isn't relying on NAV_NOISE to save it."""
    html = ('<html><body>'
            '<a href="/partners">Partners</a>'
            '<a href="/partnerships">Our Partnerships</a>'
            '</body></html>')
    jobs = run_fetch("Acme Medical", "https://acme.com/careers", html)
    assert jobs == []


def test_long_concatenated_title_is_no_longer_rejected():
    """That board's longest title + location + employment type runs to 109
    characters — past the old 100-char cap, under the new one."""
    long_title = "Machine Learning Operations Architect (with medical device experience)"
    html = "<html><body>" + _personio_style_link(1, long_title) + "</body></html>"
    jobs = run_fetch("Acme Neurotech", "https://acme-neurotech.example.com/careers", html)
    assert len(jobs) == 1
    assert len(jobs[0]["title"]) > 100


def test_length_cap_still_rejects_paragraph_length_text():
    """The cap was raised, not removed — a nav/footer blurb that happens to
    contain a hint word must still be filtered."""
    blurb = (
        "Join our engineering team and help build the next generation of "
        "medical devices that improve patient outcomes around the world "
        "every single day through relentless innovation and teamwork"
    )
    assert len(blurb) > html_generic.MAX_TITLE_LENGTH
    html = f'<html><body><a href="/about">{blurb}</a></body></html>'
    jobs = run_fetch("Acme Medical", "https://acme.com/careers", html)
    assert jobs == []


# --- non-job false positives (the "partner" widening's fallout) ------------
#
# The first real run after that fix (widened JOB_TITLE_HINTS and
# MAX_TITLE_LENGTH) surfaced 7 non-jobs in the same pass: four company
# sites' "partner" business-development CTA links; two investment firms'
# press-release headlines; and two team-bio LinkedIn links from a third.
# These tests reproduce each exact shape, with the companies anonymized.

def test_bare_partner_cta_is_rejected():
    """A real link shape: the anchor text is just 'Partner'."""
    html = '<html><body><a href="/partner">Partner</a></body></html>'
    jobs = run_fetch("Acme Instruments", "https://acmeinstruments.com/careers", html)
    assert jobs == []


def test_partner_with_us_cta_is_rejected():
    """Two companies' real link text (same phrase, different
    capitalization — NAV_NOISE is case-insensitive)."""
    html = ('<html><body>'
            '<a href="/partner-with-us">Partner with Us</a>'
            '<a href="/partner-with-us">Partner With Us</a>'
            '</body></html>')
    jobs = run_fetch("Acme Digital Health", "https://acmedigitalhealth.com/careers", html)
    assert jobs == []


def test_become_a_partner_cta_is_rejected():
    """A third company's real link text."""
    html = '<html><body><a href="/become-partner">Become a partner</a></body></html>'
    jobs = run_fetch("Acme Devices", "https://acmedevices.com/careers", html)
    assert jobs == []


def test_press_release_link_is_rejected_by_url_even_with_hint_words():
    """Two investment firms' real links: the anchor text is a news
    headline containing hint words ('Partner', 'Executive', 'Officer'), long
    enough to pass MAX_TITLE_LENGTH and NOT an exact NAV_NOISE phrase — only
    the /updates/ and /media/ URL shape catches these."""
    html = (
        '<html><body>'
        '<a href="/updates/acme-growth-partners-appoints-operating-partner/">'
        'Acme Growth Partners appoints Dana Whitfield as Operating Partner</a>'
        '<a href="/media/acme-bio-appoints-biotech-executive-as-cbo/">'
        'Acme Bio Appoints Experienced Biotech Executive, Dr Alex Rivera, as Chief '
        'Business Officer to Lead Strategic Partnering for its lead program</a>'
        '</body></html>'
    )
    jobs = run_fetch("Acme Growth Partners", "https://acmegrowth.com/careers", html)
    assert jobs == []


def test_linkedin_profile_link_is_rejected_even_with_hint_words():
    """A real link shape: a team/bio page linking out to LinkedIn
    profiles, where the bio blurb itself contains 'Executive'."""
    html = (
        '<html><body>'
        '<a href="https://www.linkedin.com/in/example-profile-b1a5/">'
        'Executive with 20+ years of medical device experience in strategy, '
        'business development, operations and commercialization</a>'
        '</body></html>'
    )
    jobs = run_fetch("Acme Advisors", "https://acmeadvisors.com/careers", html)
    assert jobs == []


def test_non_job_link_filters_dont_cost_real_postings():
    """The two filters check exact CTA phrases and specific URL path
    segments — a real posting whose title happens to contain 'partner', on
    an ordinary /jobs/ URL, must still come through."""
    html = '<html><body><a href="/jobs/42">Senior Talent Partner</a></body></html>'
    jobs = run_fetch("Acme Neurotech", "https://acme-neurotech.example.com/careers", html)
    assert len(jobs) == 1
    assert jobs[0]["title"] == "Senior Talent Partner"


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

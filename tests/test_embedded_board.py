"""Tests for embedded-board detection (punch list #19).

`extract_slug()` only reads a slug from a Careers URL already on an ATS
domain. A company hosting its careers page on its own domain had nothing to
match, so `run_scrapers()` slugified the company name instead — the
guessing invariant 7 exists to stop. Sweeping the 198 HTML-marked companies
with a Careers URL found 20 reaching a real board nothing was reading, and
10 of those have a slug the name-guess would have got wrong (`bivacor-inc`,
`fortuna-health`, `patternbioscience`, `shockwave-medical`, ...).

The danger is the mirror image of the benefit, and it is what most of these
tests are about. A careers page can name boards that are not the company's:
Atomic VC's lists its portfolio companies' boards (Exowatt, Hims & Hers)
alongside its own, and an early version of this detector confidently
returned Exowatt. Attaching another company's postings would be wrong in a
way nothing downstream could catch — the same hazard as Workday's guessed
tenant. Hence unanimity: a tier answers only if it names exactly one board,
and otherwise refuses so the caller falls back to guessing, which at least
fails visibly.

Fixtures here are reduced from the real pages named in each test.

Every HTTP call is stubbed, so the suite makes no network calls, spends
nothing, and needs no credentials.

Runs under pytest, and standalone (`python tests/test_embedded_board.py`).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ats_finder import find_embedded_board
from ats_finder.find_embedded_board import detect_embedded_board

# The real patterns run.py passes in, imported from the one place they live
# so a change there is caught here rather than drifting (invariant 9).
import run
PATTERNS = run.SLUG_URL_PATTERNS


class FakeResponse:
    def __init__(self, text, url, status=200):
        self.text = text
        self.url = url
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %d" % self.status_code)


def stub(html="", final_url=None, error=None, status=200):
    class FakeSession:
        def get(self, url, **kwargs):
            if error:
                raise error
            return FakeResponse(html, final_url or url, status)
    find_embedded_board.session = FakeSession()


def restore():
    from http_client import session as real
    find_embedded_board.session = real


def detect(html="", final_url=None, error=None, status=200,
           url="https://acme.com/careers"):
    stub(html, final_url, error, status)
    try:
        return detect_embedded_board(url, PATTERNS)
    finally:
        restore()


# --- tier 1: the saved URL redirects onto the board ------------------------

def test_a_redirect_to_the_board_is_read_from_the_final_url():
    """Eight companies work this way (BiVacor, Neko Health, Shockwave...).
    The company's own server sent us here, so it is as good as a saved
    board URL."""
    platform, slug, how, _ = detect(
        html="<html><body>Jobs</body></html>",
        final_url="https://jobs.ashbyhq.com/bivacor-inc")
    assert (platform, slug) == ("Ashby", "bivacor-inc")
    assert "redirect" in how


def test_a_redirect_slug_can_differ_from_the_company_name():
    """The whole point: slugify("BiVacor") is "bivacor", and the real board
    is "bivacor-inc". The guess would have 404'd."""
    from ats_finder.find_ats import slugify
    _, slug, _, _ = detect(final_url="https://jobs.ashbyhq.com/bivacor-inc")
    assert slug != slugify("BiVacor")


def test_canonical_url_naming_a_board_is_trusted():
    """BiVacor's careers URL serves the Ashby board's own HTML, whose
    canonical and og:url both name the board even without a redirect."""
    html = ('<html><head><link rel="canonical" href="https://job-boards.greenhouse.io/xund"/>'
            '<meta property="og:url" content="https://job-boards.greenhouse.io/xund"/>'
            '</head><body></body></html>')
    platform, slug, how, _ = detect(html=html)
    assert (platform, slug) == ("Greenhouse", "xund")
    assert "canonical" in how


# --- tier 2: a genuine embed -----------------------------------------------

def test_greenhouse_script_embed_with_the_js_segment():
    """The form actually found on every Greenhouse <script> embed. Punch
    list #19 documented `job_board?for=`, without the `/js`, which would
    have matched none of them."""
    html = ('<div id="grnhse_app"></div><script '
            'src="https://boards.greenhouse.io/embed/job_board/js?for=patternbioscience">'
            '</script>')
    platform, slug, how, _ = detect(html=html)
    assert (platform, slug) == ("Greenhouse", "patternbioscience")
    assert "embeds" in how


def test_greenhouse_iframe_embed_without_the_js_segment():
    """Apptronik's page uses the documented form in an iframe, so both
    shapes have to work."""
    html = ('<iframe id="grnhse_iframe" '
            'src="https://job-boards.greenhouse.io/embed/job_board?for=apptronik&amp;val=x">'
            '</iframe>')
    platform, slug, _, _ = detect(html=html)
    assert (platform, slug) == ("Greenhouse", "apptronik")


def test_ashby_embed_script():
    html = ('<div id="ashby_embed"></div>'
            '<script src="https://jobs.ashbyhq.com/ellipsis-health/embed?version=2"></script>')
    platform, slug, _, _ = detect(html=html)
    assert (platform, slug) == ("Ashby", "ellipsis-health")


def test_inline_js_calling_the_board_api_counts_as_an_embed():
    """Senseye's page has no embed tag at all — it fetches the Workable
    widget API from inline JS. A page calling a board's API is asserting
    that board is its own just as much as a script tag does."""
    html = ("<script>(function(){ fetch("
            "'https://apply.workable.com/api/v1/widget/accounts/senseye')"
            ".then(function(r){return r.json();}); })();</script>")
    platform, slug, _, _ = detect(html=html)
    assert (platform, slug) == ("Workable", "senseye")


def test_embed_is_not_mistaken_for_a_slug():
    """boards.greenhouse.io/embed/... also matches the generic board
    pattern, whose capture group would return "embed" — making every
    embedding page look like the same fictional company."""
    html = '<script src="https://boards.greenhouse.io/embed/job_board/js?for=delfina"></script>'
    _, slug, _, _ = detect(html=html)
    assert slug == "delfina"


# --- the guard -------------------------------------------------------------

def test_a_page_naming_several_boards_refuses_to_answer():
    """Atomic VC's careers page, reduced. An earlier version returned
    Exowatt — another company entirely."""
    html = ('<a href="https://jobs.ashbyhq.com/atomic">Atomic</a>'
            '<a href="https://jobs.lever.co/exowatt">Exowatt</a>'
            '<a href="https://jobs.ashbyhq.com/hims-and-hers">Hims &amp; Hers</a>')
    platform, slug, how, _ = detect(html=html)
    assert platform is None and slug is None, \
        "an aggregator page must refuse, not pick a portfolio company's board"
    assert "several boards" in how
    assert "exowatt" in how.lower(), "the reason should name what it saw"


def test_multiple_embeds_also_refuse():
    html = ('<script src="https://boards.greenhouse.io/embed/job_board/js?for=one"></script>'
            '<script src="https://boards.greenhouse.io/embed/job_board/js?for=two"></script>')
    platform, _, how, _ = detect(html=html)
    assert platform is None
    assert "more than one" in how


def test_a_single_link_is_accepted():
    """Novo Holdings and Guidehealth only link to their own board. One
    board named once is not an aggregator."""
    html = '<a href="https://apply.workable.com/novoholdings/">Open positions</a>'
    platform, slug, how, _ = detect(html=html)
    assert (platform, slug) == ("Workable", "novoholdings")
    assert "links to exactly one" in how


def test_an_embed_wins_over_a_link_to_a_different_board():
    """Tiers are considered separately and strongest-first, so a page that
    embeds its own board while linking elsewhere is still resolved."""
    html = ('<script src="https://boards.greenhouse.io/embed/job_board/js?for=delfina"></script>'
            '<a href="https://jobs.lever.co/someoneelse">Partner jobs</a>')
    platform, slug, _, _ = detect(html=html)
    assert (platform, slug) == ("Greenhouse", "delfina")


def test_teamtailor_is_excluded_from_detection():
    """Its pattern must match a bare careers-subdomain host, which on an
    arbitrary page matches almost any careers URL. Scanning with it
    produced one wrong answer and four false refusals against the real
    pages, so it is deliberately not used here."""
    assert "Teamtailor" in find_embedded_board.SCAN_EXCLUDED_PLATFORMS
    platform, slug, _, _ = detect(
        html='<a href="https://careers.someunrelatedcompany.com/jobs">Careers</a>')
    assert platform is None and slug is None


# --- failure behaviour -----------------------------------------------------

def test_a_fetch_failure_returns_a_reason_rather_than_raising():
    """This runs over ~180 companies inside run_scrapers. One unreachable
    careers page must not end the scrape of everything after it."""
    platform, slug, how, html = detect(error=RuntimeError("connection reset"))
    assert platform is None and slug is None and html is None
    assert "could not fetch" in how


def test_a_page_with_no_board_says_so():
    """Siemens Healthineers, Gondola and CMR Surgical are all in this
    bucket — JS-rendered listings with nothing in the initial HTML. That is
    punch list #5's territory, not a failure here."""
    platform, slug, how, html = detect(html="<html><body><h1>Careers</h1></body></html>")
    assert platform is None and slug is None
    assert "no ATS board found" in how
    assert html is not None, "the HTML still comes back for the HTML scraper to reuse"


def test_a_missing_or_unusable_url_is_handled():
    for bad in (None, "", "www.google.com", "not a url"):
        platform, slug, how, html = detect(url=bad)
        assert platform is None and html is None, bad


def test_the_fetched_html_is_returned_for_reuse():
    """run_scrapers hands this to html_generic so the same page is not
    requested twice for every one of ~180 HTML companies."""
    page = "<html><body>the page</body></html>"
    _, _, _, html = detect(html=page)
    assert html == page


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

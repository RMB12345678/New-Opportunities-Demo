"""Tests for the seven ATS scrapers added after auditing the Notion list.

The audit found 25 companies whose ATS Platform named a real platform that
no scraper handled. Rows marked "API" got no scrape attempt at all; rows
marked "HTML" fell through to scrapers/html_generic.py and reported zero
open roles for boards that were not empty. Both failures are invisible
downstream — a company with no postings and a company nobody asked looks
identical in the Job Postings database.

What is protected here is what actually bit during the build:

  - Slug extraction. Every one of these platforms puts its slug in a
    different place, and two URL shapes in Notion look like slugs but are
    not: Workable's aggregator link carries an opaque account id, and a
    Teamtailor company whose Careers URL fell back to a Google search
    would otherwise yield "www.google.com" as its hostname.
  - Personio's missing-tenant behaviour. It does not 404 — it redirects to
    the vendor's marketing site, which 429s, which the shared retry
    session then retries five times with backoff. One wrong slug became a
    slow retry storm against a site that never had the answer.
  - The job-dict contract. Nine scrapers now feed one sync; a key spelled
    differently in one of them silently loses that field for that platform
    only.
  - Per-posting detail fetches (Rippling, SmartRecruiters) must degrade,
    not raise. Losing one description is a small loss; raising discards
    every posting for that company and — via scraped_ok — makes the run
    unable to tell "gone" from "never fetched".

Every HTTP call is stubbed, so the suite makes no network calls, spends
nothing, and needs no credentials.

Runs under pytest, and standalone (`python tests/test_new_scrapers.py`).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run
from scrapers import (
    workable, personio, breezy, rippling, teamtailor, smartrecruiters, pinpoint,
)

# The key set every scraper in this pipeline must return, matching the four
# that predate these. run.py adds company_description afterwards.
JOB_KEYS = {"company", "title", "location", "url", "posted_at",
            "description", "source_ats"}


# --- stubbing --------------------------------------------------------------

class FakeResponse:
    def __init__(self, payload=None, status=200, content=b"", location=None):
        self._payload = payload
        self.status_code = status
        self.content = content
        self.headers = {"Location": location} if location else {}
        self.is_redirect = location is not None
        self.is_permanent_redirect = False

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %d" % self.status_code)


def stub(module, handler):
    """Swap a scraper module's shared session for one driving `handler`.

    Returns the list of calls made, so a test can assert on what was
    actually requested (that details=true was sent, that paging advanced)
    rather than only on what came back.
    """
    calls = []

    class FakeSession:
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return handler(url, **kwargs)

    module.session = FakeSession()
    return calls


def restore(module):
    from http_client import session as real
    module.session = real


# --- slug extraction -------------------------------------------------------

def test_slugs_are_read_from_the_real_saved_urls():
    """Every case here is a Careers URL actually stored in Notion."""
    cases = [
        ("Workable", "https://apply.workable.com/cvrx/", "cvrx"),
        ("Workable", "https://apply.workable.com/epitel/?lng=en", "epitel"),
        ("Workable", "https://apply.workable.com/archimed/#jobs", "archimed"),
        ("Personio", "https://cyted.jobs.personio.com", "cyted"),
        ("Personio", "https://inbrain-neuroelectronics.jobs.personio.com/?language=en",
         "inbrain-neuroelectronics"),
        ("Breezy", "https://isaac-health.breezy.hr", "isaac-health"),
        ("Rippling", "https://ats.rippling.com/stellaromics-inc/jobs", "stellaromics-inc"),
        ("Pinpoint", "https://astorg.pinpointhq.com/", "astorg"),
        ("SmartRecruiters", "https://careers.smartrecruiters.com/Koneksa", "Koneksa"),
        ("Teamtailor", "https://career.nordiccapital.com/", "career.nordiccapital.com"),
        ("Teamtailor", "https://careers.flowneuroscience.com/jobs",
         "careers.flowneuroscience.com"),
    ]
    for platform, url, expected in cases:
        got = run.extract_slug(url, platform)
        assert got == expected, "%s: %r -> %r, wanted %r" % (platform, url, got, expected)


def test_workable_aggregator_url_yields_no_slug():
    """Vivani's saved URL is the jobs.workable.com aggregator, whose path
    id is NOT the account slug the widget API takes. Matching it would 404
    confidently every run; yielding None lets the guess-from-the-name
    fallback run, which is what actually resolves Vivani."""
    url = ("https://jobs.workable.com/company/joUPNmrEdRuHwEjzGzfQ6U"
           "/jobs-at-vivani-medical%2C-inc")
    assert run.extract_slug(url, "Workable") is None


def test_teamtailor_does_not_mistake_the_google_fallback_for_a_host():
    """Teamtailor's slug is a whole hostname, so a pattern matching any
    URL's host would turn the Careers URL backfill's last-resort Google
    search into a board named www.google.com."""
    assert run.extract_slug(
        "https://www.google.com/search?q=Acme+careers", "Teamtailor") is None
    assert run.extract_slug(
        "https://acme.teamtailor.com/jobs", "Teamtailor") == "acme.teamtailor.com"


def test_every_new_platform_is_registered():
    for name in ("Workable", "Personio", "Breezy", "Rippling", "Teamtailor",
                 "SmartRecruiters", "Pinpoint"):
        assert name in run.SCRAPERS, "%s has no scraper registered" % name


def test_registry_keys_match_the_notion_spellings():
    """resolve_scraper matches whole words, so the registry key has to be
    the shortest unambiguous form — Notion holds both "Breezy HR" and
    "Rippling ATS", which an exact-match key would miss."""
    assert run.resolve_scraper("Breezy HR")[1] == "Breezy"
    assert run.resolve_scraper("Rippling ATS")[1] == "Rippling"
    assert run.resolve_scraper("Workable (custom domain)")[1] == "Workable"
    assert run.resolve_scraper("Pinpoint (pinpointhq.com)")[1] == "Pinpoint"
    assert run.resolve_scraper("JazzHR (applytojob.com)")[1] is None


# --- Workable --------------------------------------------------------------

WORKABLE_JOB = {
    "title": "Quality Engineer",
    "url": "https://apply.workable.com/j/ABC123",
    "shortlink": "https://apply.workable.com/j/ABC123",
    "published_on": "2026-05-01",
    "city": "Boston", "state": "Massachusetts", "country": "United States",
    "telecommuting": False,
    "description": "<p>Own the QMS.</p>",
}


def test_workable_asks_for_details_and_maps_the_posting():
    calls = stub(workable, lambda url, **kw: FakeResponse(
        {"name": "Acme", "jobs": [WORKABLE_JOB]}))
    try:
        jobs = workable.fetch_jobs("Acme", slug="acme")
    finally:
        restore(workable)

    assert "details=true" in calls[0][0], \
        "without details=true the response carries no description at all"
    assert set(jobs[0]) == JOB_KEYS
    assert jobs[0]["source_ats"] == "Workable"
    assert jobs[0]["location"] == "Boston, Massachusetts, United States"
    assert jobs[0]["description"] == "<p>Own the QMS.</p>"


def test_workable_remote_role_says_remote_rather_than_nothing():
    """A fully-remote posting has empty city/state/country. None would read
    downstream as "location unknown", which is not what the board said."""
    remote = dict(WORKABLE_JOB, city="", state="", country="", telecommuting=True)
    stub(workable, lambda url, **kw: FakeResponse({"jobs": [remote]}))
    try:
        assert workable.fetch_jobs("Acme", slug="acme")[0]["location"] == "Remote"
    finally:
        restore(workable)


def test_workable_collapses_a_posting_listed_once_per_city():
    """Workable repeats a multi-location role once per city, same
    shortcode and same apply URL each time. Six entries for ARCHIMED's
    "HR Internship (Lyon, France)" became six Notion rows for one URL,
    five of which nothing could ever visit again."""
    cities = ["Lyon", "Paris", "Geneva", "Brussels"]
    payload = {"jobs": [dict(WORKABLE_JOB, city=c, state="", country="France")
                        for c in cities]}
    stub(workable, lambda url, **kw: FakeResponse(payload))
    try:
        jobs = workable.fetch_jobs("Acme", slug="acme")
    finally:
        restore(workable)

    assert len(jobs) == 1, "one URL is one posting, however many cities it lists"
    assert jobs[0]["location"] == "Lyon, France / Paris, France / Geneva, France +1 more",         "every city the board named is kept — the first one does not just win"
    assert set(jobs[0]) == JOB_KEYS


def test_workable_keeps_genuinely_different_postings_apart():
    """The collapse keys on the apply URL, so two real roles stay two."""
    other = dict(WORKABLE_JOB, title="Regulatory Lead",
                 url="https://apply.workable.com/j/XYZ789",
                 shortlink="https://apply.workable.com/j/XYZ789")
    stub(workable, lambda url, **kw: FakeResponse({"jobs": [WORKABLE_JOB, other]}))
    try:
        jobs = workable.fetch_jobs("Acme", slug="acme")
    finally:
        restore(workable)
    assert len(jobs) == 2
    assert {j["title"] for j in jobs} == {"Quality Engineer", "Regulatory Lead"}


def test_workable_postings_with_no_url_are_not_collapsed_together():
    """A missing URL is not an identity two postings share."""
    blank = dict(WORKABLE_JOB, url=None, shortlink=None)
    stub(workable, lambda url, **kw: FakeResponse(
        {"jobs": [blank, dict(blank, title="Regulatory Lead")]}))
    try:
        jobs = workable.fetch_jobs("Acme", slug="acme")
    finally:
        restore(workable)
    assert len(jobs) == 2


def test_workable_empty_board_is_a_real_answer():
    """CVRx, Kneu, Medcura and Shockwave Medical all answer 200 with an
    empty list today. That is "nothing open", not a failure, and it has to
    return [] so the close pass can retire their stale rows."""
    stub(workable, lambda url, **kw: FakeResponse({"name": "Acme", "jobs": []}))
    try:
        assert workable.fetch_jobs("Acme", slug="acme") == []
    finally:
        restore(workable)


# --- Personio --------------------------------------------------------------

PERSONIO_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<workzag-jobs>
  <position>
    <id>2793912</id>
    <office>Remote - United Kingdom</office>
    <subcompany>Cyted Health Ltd</subcompany>
    <name>Clinical Nurse Trainer</name>
    <createdAt>2026-09-11T15:17:45+00:00</createdAt>
    <jobDescriptions>
      <jobDescription><name>About Us</name><value>We do endoscopy.</value></jobDescription>
      <jobDescription><name>Role</name><value>Train nurses.</value></jobDescription>
    </jobDescriptions>
  </position>
</workzag-jobs>
"""


def test_personio_parses_the_feed_and_builds_a_posting_url():
    """The feed carries no URL field, so it is constructed — and it has to
    be, since a posting with no URL cannot be deduped by the Notion sync."""
    stub(personio, lambda url, **kw: FakeResponse(content=PERSONIO_XML))
    try:
        jobs = personio.fetch_jobs("Cyted Health", slug="cyted")
    finally:
        restore(personio)

    assert len(jobs) == 1
    job = jobs[0]
    assert set(job) == JOB_KEYS
    assert job["source_ats"] == "Personio"
    assert job["title"] == "Clinical Nurse Trainer"
    assert job["url"] == "https://cyted.jobs.personio.com/job/2793912"
    assert job["location"] == "Remote - United Kingdom", \
        "office is the location; subcompany is the legal entity, not a place"
    assert "We do endoscopy." in job["description"]
    assert "Train nurses." in job["description"]


def test_personio_missing_tenant_fails_fast_without_chasing_the_redirect():
    """A tenant that does not exist answers 307 -> https://personio.com,
    which answers 429. Following it means http_client's retry session
    retries the vendor's marketing site five times with growing backoff,
    and the Scrape Note ends up reading "too many 429 error responses"
    with no mention of the board. This is the regression guard."""
    def handler(url, **kwargs):
        assert kwargs.get("allow_redirects") is False, \
            "redirects must not be followed — that is the whole fix"
        return FakeResponse(status=307, location="https://personio.com")

    stub(personio, handler)
    try:
        personio.fetch_jobs("CoMind", slug="comind")
    except Exception as e:
        assert "does not exist" in str(e) or "redirects to" in str(e), \
            "the error must name the real problem, not a 429 from elsewhere"
    else:
        raise AssertionError("a missing Personio tenant must raise")
    finally:
        restore(personio)


def test_personio_falls_back_to_the_de_host():
    """The tld is part of a tenant's configuration, not a mirror."""
    seen = []

    def handler(url, **kwargs):
        seen.append(url)
        if "personio.com" in url:
            return FakeResponse(status=404)
        return FakeResponse(content=PERSONIO_XML)

    stub(personio, handler)
    try:
        jobs = personio.fetch_jobs("Acme", slug="acme")
    finally:
        restore(personio)

    assert len(jobs) == 1
    assert jobs[0]["url"].endswith("personio.de/job/2793912")
    assert any("personio.com" in u for u in seen), ".com is tried first"


# --- Breezy ----------------------------------------------------------------

def test_breezy_reads_a_bare_array_and_flags_remote():
    payload = [{
        "name": "Care Navigator",
        "url": "https://isaac-health.breezy.hr/p/abc",
        "published_date": "2026-09-17T19:21:39.625Z",
        "location": {"name": "Dominican Republic", "is_remote": True},
    }]
    stub(breezy, lambda url, **kw: FakeResponse(payload))
    try:
        jobs = breezy.fetch_jobs("Isaac Health", slug="isaac-health")
    finally:
        restore(breezy)

    assert set(jobs[0]) == JOB_KEYS
    assert jobs[0]["source_ats"] == "Breezy HR"
    assert jobs[0]["location"] == "Dominican Republic (remote)"


# --- Rippling --------------------------------------------------------------

RIPPLING_LIST = [{
    "uuid": "u-1", "name": "Accounting Manager",
    "url": "https://ats.rippling.com/acme/jobs/u-1",
    "workLocation": {"label": "Boston, MA"},
}]


def test_rippling_fetches_the_description_from_the_detail_endpoint():
    def handler(url, **kwargs):
        if url.endswith("/jobs"):
            return FakeResponse(RIPPLING_LIST)
        return FakeResponse({
            "description": {"company": "About Acme.", "role": "Own the ledger."},
            "createdOn": "2026-08-10T10:28:24-07:00",
            "workLocations": ["Boston, MA"],
        })

    stub(rippling, handler)
    try:
        jobs = rippling.fetch_jobs("Acme", slug="acme")
    finally:
        restore(rippling)

    assert set(jobs[0]) == JOB_KEYS
    assert jobs[0]["source_ats"] == "Rippling"
    assert jobs[0]["description"].startswith("Own the ledger."), \
        "role leads: scoring truncates, so company boilerplate must not go first"
    assert jobs[0]["posted_at"].startswith("2026-08-10")


def test_rippling_keeps_the_posting_when_its_detail_call_fails():
    """One posting's detail failing is not the board failing. Raising here
    would discard every posting for the company, and scraped_ok would then
    be unable to tell "gone" from "never fetched"."""
    def handler(url, **kwargs):
        if url.endswith("/jobs"):
            return FakeResponse(RIPPLING_LIST)
        raise RuntimeError("boom")

    stub(rippling, handler)
    try:
        jobs = rippling.fetch_jobs("Acme", slug="acme")
    finally:
        restore(rippling)

    assert len(jobs) == 1, "the posting survives its missing description"
    assert jobs[0]["title"] == "Accounting Manager"
    assert jobs[0]["description"] is None
    assert jobs[0]["location"] == "Boston, MA", "the list already told us this"


# --- SmartRecruiters -------------------------------------------------------

def test_smartrecruiters_pages_past_the_first_hundred():
    """limit is capped at 100 server-side, so a 250-posting board that
    returns 100 looks exactly like a small company unless the loop is
    right — the same failure shape as Workday's 20-job pages."""
    total = 250

    def handler(url, **kwargs):
        if "/postings/" in url:
            return FakeResponse({"jobAd": {"sections": {}}, "postingUrl": "https://x/1"})
        offset = kwargs["params"]["offset"]
        limit = kwargs["params"]["limit"]
        content = [{"id": str(i), "name": "Job %d" % i, "location": {}}
                   for i in range(offset, min(offset + limit, total))]
        return FakeResponse({"offset": offset, "limit": limit,
                             "totalFound": total, "content": content})

    calls = stub(smartrecruiters, handler)
    try:
        jobs = smartrecruiters.fetch_jobs("Acme", slug="acme")
    finally:
        restore(smartrecruiters)

    assert len(jobs) == total
    listing = [c for c in calls if "/postings/" not in c[0]]
    assert [c[1]["params"]["offset"] for c in listing] == [0, 100, 200]


def test_smartrecruiters_never_writes_an_api_url_into_the_posting():
    """The list's `ref` is the API URL, not a page a human can open.
    Putting it in Notion would fill the dashboard with links to JSON."""
    def handler(url, **kwargs):
        if "/postings/" in url:
            return FakeResponse({
                "jobAd": {"sections": {
                    "jobDescription": {"title": "Job Description",
                                       "text": "<p>Do it.</p>"}}},
                "postingUrl": "https://jobs.smartrecruiters.com/Acme/744",
            })
        return FakeResponse({
            "offset": 0, "limit": 100, "totalFound": 1,
            "content": [{"id": "744", "name": "Analyst",
                         "ref": "https://api.smartrecruiters.com/v1/companies/acme/postings/744",
                         "location": {"fullLocation": "Poland, Remote"},
                         "releasedDate": "2026-09-09T09:43:26.403Z"}],
        })

    stub(smartrecruiters, handler)
    try:
        jobs = smartrecruiters.fetch_jobs("Acme", slug="acme")
    finally:
        restore(smartrecruiters)

    assert set(jobs[0]) == JOB_KEYS
    assert jobs[0]["url"] == "https://jobs.smartrecruiters.com/Acme/744"
    assert "api.smartrecruiters.com" not in jobs[0]["url"]
    assert "Do it." in jobs[0]["description"]


# --- Teamtailor ------------------------------------------------------------

def test_teamtailor_joins_every_advertised_city():
    """jobLocation is a list — a role can be advertised in several cities.
    Reporting only the first makes a three-city role look local."""
    payload = {"items": [{
        "title": "QA Engineer",
        "url": "https://careers.acme.com/jobs/1",
        "date_published": "2026-09-09T11:03:05+02:00",
        "content_html": "<p>Test things.</p>",
        "_jobposting": {"jobLocation": [
            {"address": {"addressLocality": "Malmo", "addressCountry": "SE"}},
            {"address": {"addressLocality": "London", "addressCountry": "GB"}},
        ]},
    }]}
    stub(teamtailor, lambda url, **kw: FakeResponse(payload))
    try:
        jobs = teamtailor.fetch_jobs("Acme", slug="careers.acme.com")
    finally:
        restore(teamtailor)

    assert set(jobs[0]) == JOB_KEYS
    assert jobs[0]["source_ats"] == "Teamtailor"
    assert jobs[0]["location"] == "Malmo, SE / London, GB"


def test_teamtailor_guesses_inside_the_vendor_namespace():
    """With no host from the saved URL the guess is {slug}.teamtailor.com —
    safe for the same reason a Greenhouse slug guess is: a wrong guess 404s
    inside the vendor's namespace instead of resolving to someone else's
    board."""
    calls = stub(teamtailor, lambda url, **kw: FakeResponse({"items": []}))
    try:
        teamtailor.fetch_jobs("Acme Medical")
    finally:
        restore(teamtailor)
    assert calls[0][0] == "https://acmemedical.teamtailor.com/jobs.json"


# --- Pinpoint --------------------------------------------------------------

def test_pinpoint_joins_the_body_sections_and_skips_the_deadline():
    """Pinpoint publishes no start date — only deadline_at, which is when
    the posting closes. Filling posted_at from it would be wrong in both
    direction and meaning."""
    payload = {"data": [{
        "title": "PE Internship",
        "url": "https://astorg.pinpointhq.com/en/postings/abc",
        "deadline_at": "2026-11-30T23:59:59+01:00",
        "description": "<p>Astorg in a nutshell.</p>",
        "key_responsibilities_header": "Day-to-day responsibilities",
        "key_responsibilities": "<ul><li>Build models</li></ul>",
        "skills_knowledge_expertise": "",
        "benefits": "",
        "workplace_type_text": "Onsite",
        "location": {"name": "Frankfurt", "city": "Frankfurt am Main"},
    }]}
    stub(pinpoint, lambda url, **kw: FakeResponse(payload))
    try:
        jobs = pinpoint.fetch_jobs("Astorg", slug="astorg")
    finally:
        restore(pinpoint)

    job = jobs[0]
    assert set(job) == JOB_KEYS
    assert job["source_ats"] == "Pinpoint"
    assert job["posted_at"] is None
    assert job["location"] == "Frankfurt (Onsite)"
    assert "Astorg in a nutshell." in job["description"]
    assert "Day-to-day responsibilities" in job["description"]
    assert "Build models" in job["description"]


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

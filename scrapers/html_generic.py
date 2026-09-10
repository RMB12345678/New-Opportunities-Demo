"""
Best-effort scraper for company career pages that don't run a known ATS.

There's no universal structure for these pages, so this uses heuristics:
  1. Fetch the page.
  2. Look for repeated elements that resemble job listings — either
     <a> tags whose text looks like a job title, or common container
     patterns (class/id containing "job", "position", "opening", "career",
     "role").
  3. De-duplicate and filter obvious non-jobs (nav links, footer links).

This will NOT work on every site — many in-house pages render their job
list via JavaScript after the page loads, which a plain HTTP fetch can't
see. Those need a per-company override (see OVERRIDES below) or a
headless-browser tool, which isn't included here. Anything this scraper
can't confidently parse is left for output/skipped_companies.json rather
than guessing and returning junk.

Text extraction uses BeautifulSoup's get_text(" ", strip=True) — WITH an
explicit space separator, not the default get_text(strip=True). Without
it, adjacent inline elements (a common layout: title span, then location
span, then employment-type span, all siblings inside one <a>) get their
trimmed text joined with NOTHING between them, e.g. "Data Architect" next
to "Barcelona" becomes the single run "Data ArchitectBarcelona". That
silently breaks JOB_TITLE_HINTS' \b word-boundary check whenever the hint
word is the last word of the title (i.e. most of the time) — the boundary
after "Architect"/"Coordinator"/"Technician"/"Designer" only exists if
what follows is non-word text (a space, a parenthesis), and a location
name glued on with no separator is not that. On one medtech company's
board this was undercounting its real 9 open roles to 3, invisibly — no exception, no
zero-jobs flag, just most titles failing to look like titles. It is a
layout pattern common enough (Personio uses it; plenty of others do too)
that this was very likely undercounting other HTML-scraped companies the
same way, not just this one.

Two more things were tightened at the same time, both discovered on that
same board, both silent (no error, just a link that never made it
into `jobs`):

  - JOB_TITLE_HINTS was missing "partner" ("Senior Talent Partner" matched
    nothing). Added it as a whole word, so it still won't fire on a nav
    link reading "Partners" or "Partnership" (\b requires a boundary right
    after "partner", and 's'/'sh' are word characters, not boundaries).

  - MAX_TITLE_LENGTH was 100, sized for a bare title. Now that the
    extracted text is title + location + employment type joined with
    spaces (see above), a real, single job can legitimately run past 100
    — the longest on that board is 109. Raised to 160: generous enough to keep
    "Title (parenthetical) City Full-time Permanent employee" shapes, not
    so generous that it starts accepting paragraph-length nav/footer text
    instead of a listing.

One shape of "can't confidently parse" deserves its own check rather than
falling out of the heuristics as a quiet zero: a saved Careers URL that is
actually a multi-tenant ATS's own marketing/search domain rather than a
company-specific board (e.g. "https://jobs.workable.com" instead of
"https://apply.workable.com/<company>/"). That page is a real, fetchable
200 with real HTML on it, so nothing about the request fails — it just has
no job-title-shaped links on it, and run.py's "zero jobs is a trustworthy
success" rule (deliberate for a real company board that genuinely has no
openings right now) then stamps it OK. That combination — plausible URL,
wrong company, silent zero — is exactly what let one PE firm's Careers
URL sit as "https://jobs.workable.com" reporting "HTML: 0 job(s)" instead of
ever surfacing in the Needs Manual Check view. _reject_bare_ats_domain
catches this one shape by name: known multi-tenant ATS host, no
company-specific path segment. It is deliberately narrow — it is not a
general "zero jobs is suspicious" rule, which would fight the documented
reason zero is trusted elsewhere.

The fix above (widening JOB_TITLE_HINTS and MAX_TITLE_LENGTH) traded
one failure mode for another. _generic_extract scans every <a> on the page
for a hint word, with nothing requiring the link to actually sit inside a
jobs listing — so a page containing any anchor whose text has "partner" or
"executive" in it, anywhere on the page, was already a soft spot. Widening
the net made it worse, and the first run after that fix surfaced it: 7 of
52 "new" postings were not jobs —

  - Four company sites contributed a business-development CTA link —
    "Become a partner", "Partner with Us", "Partner With Us", a bare
    "Partner" — all caught directly by the new "partner" hint.
  - Two investment firms contributed a press-release headline apiece,
    of the "<Firm> appoints <Name> as Operating Partner" and "<Company>
    Appoints Experienced Biotech Executive, Dr <Name>, as Chief Business
    Officer..." shape, pulled off their /updates/ and /media/ sections.
    Both are 130+ characters and would have been rejected under the old
    100-char cap; raising it to 160 let them through.
  - One more contributed two LinkedIn profile links ("Executive with 20+
    years...") pulled off what's evidently a team/bio page, not a jobs
    page.

No title-text heuristic distinguishes "Senior Talent Partner" from "Become
a partner" — both are short phrases containing the hint word "partner".
The fix is two narrow, independent filters, neither of which loosens
JOB_TITLE_HINTS or MAX_TITLE_LENGTH back down (that would re-break the
undercounted board above):

  - NAV_NOISE now also rejects a small set of known non-job CTA phrases
    ("partner", "partner with us", "become a partner") by exact match,
    the same way it already rejects "Careers" — these are common enough
    across company sites to name directly rather than pattern-match.
  - _looks_like_non_job_link rejects by the link's destination rather
    than the anchor text: linkedin.com (never a job posting — the
    Personio board above does carry other people's LinkedIn profiles in
    its own links, but never with a JOB_TITLE_HINTS-shaped anchor text,
    so this doesn't cost real postings there) and any URL whose path contains a /media/, /news/,
    /press/, /updates/, or /blog/ segment — the shape a press release or
    company-news post lives at, and a shape a real job listing does not.
"""
import re
from urllib.parse import urlparse
from bs4 import BeautifulSoup

from http_client import session

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MedTechTargetScraper/1.0)"}

# Multi-tenant ATS platforms whose own root/marketing domain looks like a
# valid, fetchable page but is never a specific company's job board — the
# real board always has a company slug in the path (apply.workable.com/
# <company>/, boards.greenhouse.io/<company>, jobs.lever.co/<company>,
# jobs.ashbyhq.com/<company>, <tenant>.myworkdayjobs.com/...). If the saved
# Careers URL resolves to one of these hosts with no path, it was saved
# wrong — see _reject_bare_ats_domain below.
BARE_ATS_HOSTS = (
    "workable.com",
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "myworkdayjobs.com",
    "smartrecruiters.com",
    "comeet.co",
    "recruitee.com",
)


def _reject_bare_ats_domain(company_name, careers_url):
    """Raise if careers_url is a known ATS's own domain with no
    company-specific path — see the module docstring for why this needs
    its own check instead of relying on the zero-jobs case."""
    parsed = urlparse(careers_url)
    host = (parsed.hostname or "").lower()
    has_path = bool([seg for seg in parsed.path.split("/") if seg])
    if has_path:
        return
    matched_host = next((h for h in BARE_ATS_HOSTS if host == h or host.endswith("." + h)), None)
    if matched_host:
        raise ValueError(
            f"Careers URL for {company_name} ({careers_url}) is {matched_host}'s own "
            f"generic domain, not a company-specific board — it has no path segment "
            f"identifying {company_name}. Find the real company page (e.g. "
            f"apply.workable.com/<slug>/, boards.greenhouse.io/<slug>, "
            f"jobs.lever.co/<slug>) and save that instead."
        )

# Words that make an <a> tag's text look like a real job posting.
JOB_TITLE_HINTS = re.compile(
    r"\b(engineer|manager|director|specialist|coordinator|analyst|"
    r"scientist|associate|lead|head of|vp|svp|chief|president|nurse|"
    r"technician|representative|executive|officer|administrator|"
    r"designer|developer|architect|counsel|recruiter|intern|partner)\b",
    re.IGNORECASE,
)

# Sized for "Title (parenthetical) City Full-time Permanent employee" — see
# the module docstring for why a bare-title-sized cap started rejecting
# real single postings once location/employment text joined the title.
MAX_TITLE_LENGTH = 160

# Words/phrases that mean an <a> tag is almost certainly NOT a job posting,
# even if it happens to contain a hint word above (e.g. "Careers" nav link,
# or a "Become a partner" business-development CTA — "partner" is a real
# JOB_TITLE_HINTS word too, see the module docstring).
NAV_NOISE = re.compile(
    r"^(home|about|contact|careers|jobs|blog|news|privacy|terms|login|"
    r"sign in|apply now|view all|see all|learn more|partner|"
    r"partner with us|become an? partner)$",
    re.IGNORECASE,
)

# Link destinations that are never a job posting, whatever the anchor text
# says — a press release, a news post, or a team bio page can easily
# contain a JOB_TITLE_HINTS word (see the module docstring for the real
# cases). Checked against the resolved href, not the visible
# text, so it catches these regardless of phrasing.
NON_JOB_LINK_HOSTS = ("linkedin.com",)
NON_JOB_LINK_PATH_SEGMENTS = frozenset({"media", "news", "press", "updates", "blog"})


def _looks_like_non_job_link(href):
    if not href:
        return False
    parsed = urlparse(href)
    host = (parsed.hostname or "").lower()
    if any(host == h or host.endswith("." + h) for h in NON_JOB_LINK_HOSTS):
        return True
    path_segments = {seg.lower() for seg in parsed.path.split("/") if seg}
    return bool(path_segments & NON_JOB_LINK_PATH_SEGMENTS)


# Per-company overrides for sites the generic heuristics can't handle
# (e.g. JavaScript-rendered listings, unusual page structure). Add entries
# here as you find companies the generic scraper misses. Each value is a
# CSS selector (passed to BeautifulSoup's .select()) for the elements that
# contain each job's title text.
OVERRIDES = {
    # "Company Name": "css.selector.for.job.title.elements",
}


def fetch_jobs(company_name, careers_url):
    if not careers_url:
        raise ValueError(f"No careers_url provided for {company_name}")
    _reject_bare_ats_domain(company_name, careers_url)

    resp = session.get(careers_url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    if company_name in OVERRIDES:
        elements = soup.select(OVERRIDES[company_name])
        titles = [el.get_text(" ", strip=True) for el in elements]
        links = [el.get("href") if el.name == "a" else None for el in elements]
    else:
        titles, links = _generic_extract(soup, careers_url)

    jobs = []
    seen = set()
    for title, link in zip(titles, links):
        title = title.strip()
        if not title or title.lower() in seen:
            continue
        seen.add(title.lower())
        jobs.append({
            "company": company_name,
            "title": title,
            "location": None,  # generic scraper doesn't reliably find this
            "url": link if link else careers_url,
            "posted_at": None,
            "description": None,  # generic scraper has no reliable way to find posting text
            "source_ats": "HTML (generic scrape)",
        })

    return jobs


def _generic_extract(soup, base_url):
    titles, links = [], []

    for a in soup.find_all("a"):
        text = a.get_text(" ", strip=True)
        if not text or len(text) > MAX_TITLE_LENGTH:
            continue
        if NAV_NOISE.match(text):
            continue
        if not JOB_TITLE_HINTS.search(text):
            continue

        href = a.get("href")
        if href and href.startswith("/"):
            from urllib.parse import urljoin
            href = urljoin(base_url, href)

        if _looks_like_non_job_link(href):
            continue

        titles.append(text)
        links.append(href)

    return titles, links

"""Find the real ATS slug on a company's own careers page (punch list #19).

`extract_slug()` in run.py reads a slug out of a saved Careers URL, but only
when that URL is itself on an ATS domain. When a company hosts its careers
page on its own domain, there is nothing to match, and `run_scrapers()`
falls back to slugifying the company name — the exact guessing that
invariant 7 exists to stop ("guessing was why verified companies 404'd
every run"). Nothing ever looked at the page itself.

Confirmed by sweeping the 198 HTML-marked companies that have a Careers
URL: **20** of them reach a real ATS board that nothing was reading. They
come in three shapes, and this module handles them in descending order of
confidence.

1. **The saved URL redirects to the board.** Eight companies (BiVacor,
   Fortuna Health, Freed, Sevaro, Neko Health, XUND, Shockwave Medical,
   Hyro). Following the redirect and re-reading the final URL is as
   reliable as a saved board URL, because the company's own server is what
   pointed here.
2. **A genuine embed.** Seven companies, as a `<script>`/`<iframe>` src, an
   Ashby `/embed` preload, or inline JS calling the board's API (Senseye
   fetches `apply.workable.com/api/v1/widget/accounts/senseye` directly).
   A page embedding a board is asserting that board is its own.
3. **A plain link** to the company's own board (Novo Holdings, Guidehealth).
   Weakest, and the reason for the guard below.

**The guard, which is the whole safety story.** A page can link to boards
that are not the company's. Atomic VC's careers page is a portfolio jobs
aggregator listing Exowatt, Hims & Hers and a dozen others alongside its
own Ashby board; Cellino's links out to Osmo's. Picking one would attach
another company's postings to this company — the same failure Workday's
"a guessed tenant can resolve to a real board belonging to someone else"
warning is about, and one nothing downstream could detect.

So each tier is considered separately, strongest first, and a tier only
answers if it is **unanimous**: exactly one distinct (platform, slug) pair.
Two candidates in a tier means the page is an aggregator, and the honest
answer is None — let the caller fall back to guessing, which at least fails
visibly. Unanimity, not a scoring heuristic, because "most links point at
X" is exactly the reasoning that would confidently return Exowatt.

This only sees what is in the initial HTML. A board rendered entirely by
client-side JavaScript leaves nothing to find (Siemens Healthineers,
Gondola and CMR Surgical are all in that bucket — CMR Surgical is the
company that originally motivated this item and it is *not* an example of
it). That needs a headless browser, which is punch list #5.
"""
import re
from urllib.parse import urlparse, unquote

from bs4 import BeautifulSoup

from http_client import third_party_session as session

# Some careers pages return a stub or a 403 to a bare programmatic request.
# The pages are public; this just looks like a browser so they answer
# normally — the same reasoning scrapers/workday.py documents.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )
}

# Embed-specific forms, which carry the slug somewhere extract_slug's
# board-URL patterns would not look.
#
# NOTE the `/js` segment in the Greenhouse form. Punch list #19 wrote this
# pattern as `boards.greenhouse.io/embed/job_board?for=<slug>`, and that
# shape does exist — Apptronik's iframe uses it — but every <script> embed
# found in the sweep was `embed/job_board/js?for=<slug>`. Matching only the
# documented form would have missed all of them.
EMBED_PATTERNS = [
    ("Greenhouse", r"(?:job-)?boards\.greenhouse\.io/embed/job_board(?:/js)?\?for=([a-zA-Z0-9\-_]+)"),
    ("Ashby", r"jobs\.ashbyhq\.com/([a-zA-Z0-9\-_%]+)/embed"),
    ("Workable", r"apply\.workable\.com/api/v\d+/widget/accounts/([a-zA-Z0-9\-_]+)"),
]

# Attributes that carry a URL the page is *loading*, as opposed to linking.
_EMBEDDING_ATTRS = (("script", "src"), ("iframe", "src"), ("link", "href"))

# Platforms whose SLUG_URL_PATTERNS must not be used for detection.
#
# Teamtailor is the only one, and it is not an oversight in its pattern.
# Teamtailor customers run on their own domains, so its pattern has to
# match a bare careers-subdomain host (`careers.acme.com`). That is correct
# when reading a slug from a Careers URL already known to be Teamtailor's,
# and actively harmful when sweeping arbitrary page HTML, where it matches
# every `careers.*`/`jobs.*` URL a page happens to mention. Measured against
# the 20 real pages: it produced one outright wrong answer (Cellino's page
# redirects to the aggregator jobs.engine.xyz, which it read as a Teamtailor
# board) and four false "more than one board" refusals that hid the correct
# Ashby and Greenhouse answers underneath.
#
# The cost of excluding it is real and accepted: a Teamtailor board embedded
# in a page on a custom domain cannot be found this way. Detecting it would
# mean trusting a pattern that matches almost any careers URL, which is a
# worse trade than missing it.
SCAN_EXCLUDED_PLATFORMS = {"Teamtailor"}


def _scannable(slug_patterns):
    """slug_patterns minus the platforms too generic to scan HTML with."""
    return {p: pats for p, pats in slug_patterns.items()
            if p not in SCAN_EXCLUDED_PLATFORMS}


def _slugs_from_text(text, slug_patterns):
    """Every (platform, slug) a blob of text mentions, embed forms first."""
    out = []
    for platform, pattern in EMBED_PATTERNS:
        for match in re.findall(pattern, text, re.I):
            out.append((platform, unquote(match)))
    for platform, patterns in slug_patterns.items():
        for pattern in patterns:
            for match in re.findall(pattern, text, re.I):
                slug = match if isinstance(match, str) else match[0]
                # "embed" is a path segment in boards.greenhouse.io/embed/...,
                # not a board name; without this the generic board pattern
                # returns it and every embedding page looks like the same
                # fictional company.
                if slug and slug.lower() not in ("embed", "job_board", "js"):
                    out.append((platform, unquote(slug)))
    return out


def _unanimous(candidates):
    """One distinct (platform, slug), or None.

    Deliberately not "the most common" — an aggregator page linking eight
    portfolio boards and one of its own would answer with whichever
    appeared most, which is how Atomic VC's page yields Exowatt.
    """
    distinct = {(p, s) for p, s in candidates}
    return distinct.pop() if len(distinct) == 1 else None


def detect_embedded_board(careers_url, slug_patterns, timeout=20):
    """Return (platform, slug, how, html) for the board this page really
    uses, or (None, None, reason, html).

    `html` is the page this fetched, or None if the fetch failed. It is
    handed back so the caller can pass it to scrapers/html_generic.py
    instead of fetching the same page a second time — this runs over every
    HTML-marked company, ~180 of them, and a duplicate request each would
    add minutes to a run that is already sequential (punch list #13).

    `slug_patterns` is run.py's SLUG_URL_PATTERNS, passed in rather than
    imported: run.py already imports this package, so importing it back
    would be circular, and restating the patterns here would leave two
    records of one fact to drift apart (invariant 9).

    Raises nothing on a fetch failure — it returns a reason instead. A
    company whose careers page is briefly unreachable should fall back to
    the existing guess, not blow up the scrape of every company after it.
    """
    if not careers_url or not careers_url.startswith(("http://", "https://")):
        return None, None, "no usable Careers URL to fetch", None

    slug_patterns = _scannable(slug_patterns)

    try:
        resp = session.get(careers_url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        html = resp.text
        final_url = resp.url
    except Exception as e:
        return None, None, f"could not fetch the careers page ({e})", None

    # --- Tier 1: the page redirected onto the board itself.
    for platform, patterns in slug_patterns.items():
        for pattern in patterns:
            m = re.search(pattern, final_url)
            if m:
                return platform, unquote(m.group(1)), f"the Careers URL redirects to {final_url}", html

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:
        soup = None

    # A board's own page often declares itself even without a redirect —
    # BiVacor's careers URL serves the Ashby board's HTML, whose canonical
    # and og:url both name the board. That is the site telling us directly.
    if soup is not None:
        declared = []
        canonical = soup.find("link", rel=lambda v: v and "canonical" in v)
        if canonical and canonical.get("href"):
            declared += _slugs_from_text(canonical["href"], slug_patterns)
        og = soup.find("meta", property="og:url")
        if og and og.get("content"):
            declared += _slugs_from_text(og["content"], slug_patterns)
        answer = _unanimous(declared)
        if answer:
            return answer[0], answer[1], "the page declares that board as its canonical URL", html

    # --- Tier 2: a genuine embed — something the page LOADS, plus inline JS.
    embedded = []
    if soup is not None:
        for tag_name, attr in _EMBEDDING_ATTRS:
            for tag in soup.find_all(tag_name):
                value = tag.get(attr)
                if value:
                    embedded += _slugs_from_text(value, slug_patterns)
        for script in soup.find_all("script"):
            if script.string:
                for platform, pattern in EMBED_PATTERNS:
                    for match in re.findall(pattern, script.string, re.I):
                        embedded.append((platform, unquote(match)))
    # Inline JS is not always inside a <script> the parser keeps, and some
    # pages build the URL by concatenation, so sweep the raw HTML for the
    # embed-specific forms too. These are unambiguous enough to trust:
    # nothing links to another company's *embed* endpoint.
    for platform, pattern in EMBED_PATTERNS:
        for match in re.findall(pattern, html, re.I):
            embedded.append((platform, unquote(match)))

    answer = _unanimous(embedded)
    if answer:
        return answer[0], answer[1], "the page embeds that board", html
    if embedded:
        distinct = sorted({f"{p}:{s}" for p, s in embedded})
        return None, None, (f"the page embeds more than one board ({', '.join(distinct[:4])}) "
                            f"— looks like an aggregator, refusing to guess"), html

    # --- Tier 3: a plain link, only if the page names exactly one board.
    linked = []
    if soup is not None:
        for tag in soup.find_all("a"):
            if tag.get("href"):
                linked += _slugs_from_text(tag["href"], slug_patterns)
    answer = _unanimous(linked)
    if answer:
        return answer[0], answer[1], "the page links to exactly one board", html
    if linked:
        distinct = sorted({f"{p}:{s}" for p, s in linked})
        return None, None, (f"the page links to several boards ({', '.join(distinct[:4])}) "
                            f"— cannot tell which is this company's, refusing to guess"), html

    return None, None, "no ATS board found in the page's HTML", html

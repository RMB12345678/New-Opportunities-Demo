"""
Auto-detects a company's ATS platform from its careers page, the same way
this was done manually while building the initial target list.

Strategy:
1. Try a handful of known URL patterns directly (fast path, no API cost):
   Greenhouse, Lever, Ashby, Workable, Comeet, SmartRecruiters, Recruitee.
2. If none match, fall back to the Anthropic API with the web_search tool
   to search for "<company> careers" and classify the result.

Requires env var ANTHROPIC_API_KEY for the fallback step.
"""
import os
import re
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from http_client import session

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

# (platform name, url template, scrape method) — tried in order
KNOWN_PATTERNS = [
    ("Greenhouse", "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", "API"),
    ("Lever", "https://api.lever.co/v0/postings/{slug}?mode=json", "API"),
    ("Ashby", "https://api.ashbyhq.com/posting-api/job-board/{slug}", "API"),
]

# try_known_patterns() fires up to 3 speculative GETs per company against a
# GUESSED slug and expects most to 404 — that's the normal case, not a
# transient failure. The shared `session` above is tuned for calls that are
# actually likely to succeed (total=5, backoff_factor=1.5); reusing it here
# would turn a routine 503 on a wrong guess into a ~45s wait, three guesses
# per company, across the whole target list. This session fails fast
# instead: one retry, no backoff pileup.
_guess_retry = Retry(
    total=1,
    backoff_factor=1.5,
    status_forcelist=[429, 500, 502, 503, 504, 529],
    allowed_methods=["GET"],
    respect_retry_after_header=True,
)
_guess_session = requests.Session()
_guess_session.mount("https://", HTTPAdapter(max_retries=_guess_retry))
_guess_session.mount("http://", HTTPAdapter(max_retries=_guess_retry))


def slugify(company_name):
    return re.sub(r"[^a-z0-9]", "", company_name.lower())


def try_known_patterns(company_name):
    """Quick, free check against common ATS API slugs before spending an
    LLM call. Many companies use their name (or a close variant) as the
    slug, so this catches a meaningful fraction of cases for free."""
    slug = slugify(company_name)
    for platform, template, method in KNOWN_PATTERNS:
        url = template.format(slug=slug)
        try:
            resp = _guess_session.get(url, timeout=6)
            if resp.status_code == 200 and resp.json():
                return platform, method
        except requests.RequestException:
            continue
    return None, None


# --- sniffing a careers page for a branded-front-end ATS --------------------
# Large companies often put their own branded page in front of a standard ATS,
# so the research step reads "in-house" while the page source names the real
# one. This is free (one GET, no API call) and runs before an in-house or
# not-found answer is accepted.
#
# Each pattern captures enough to name a distinct board, because the rule is
# the one find_embedded_board.py learned the hard way (invariant 19): a page
# may name SEVERAL boards, and attaching the wrong one is worse than saying
# nothing. So this answers only when the page names exactly one platform AND
# one board of it. Taleo (taleo.net) is deliberately not detected — it is a
# different product with a different API, and mislabeling it would route it
# to a scraper that cannot read it.
_SNIFF_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
}

# platform name as written to ATS Platform -> (scrape method, [(regex, board-url builder)])
_SNIFF_PATTERNS = [
    ("Workday", "API",
     re.compile(r"https?://([a-z0-9\-]+\.wd\d+)\.myworkdayjobs\.com"
                r"(?:/[a-z]{2}-[A-Z]{2})?/([A-Za-z0-9_\-]+)", re.I),
     lambda m: f"https://{m.group(1)}.myworkdayjobs.com/{m.group(2)}"),
    ("Oracle Recruiting Cloud", "API",
     re.compile(r"https?://([a-z0-9.\-]+\.oraclecloud\.com)/hcmUI/CandidateExperience/"
                r"[A-Za-z\-]+/sites/([A-Za-z0-9_\-]+)", re.I),
     lambda m: f"https://{m.group(1)}/hcmUI/CandidateExperience/en/sites/{m.group(2)}"),
    ("iCIMS", "HTML",
     re.compile(r"https?://((?!static\.|www\.|content\.|assets\.|cdn\.|img\.|images\.|"
                r"login\.|app\.|api\.|support\.)[a-z0-9\-]+\.icims\.com)", re.I),
     lambda m: f"https://{m.group(1)}"),
    # career5.successfactors.com/career?company=X is the JS-rendered shape;
    # rmkcdn.successfactors.com is the CDN of the branded "RMK" career-site
    # template, which is how a custom-domain SuccessFactors site gives itself away.
    ("SuccessFactors", "HTML",
     re.compile(r"https?://((?:career\d*|rmkcdn|jobs|performancemanager\d*)"
                r"\.(?:successfactors\.(?:com|eu)|sapsf\.(?:com|eu))"
                r"(?:/career\?[^\s\"'<>\]*company=[A-Za-z0-9_\-]+)?)", re.I),
     lambda m: m.group(0)),
]

_UNRESOLVED_ATS = re.compile(
    r"\b(none|in-?house|not found|custom|proprietary|manual)\b", re.IGNORECASE)


def ats_is_unresolved(ats_text):
    """True when the research answer amounts to 'no standard ATS' — the
    answers worth double-checking against the page source."""
    return bool(ats_text) and bool(_UNRESOLVED_ATS.search(ats_text))


def sniff_ats_from_page(careers_url, html=None):
    """Look in a careers page's source (and where it redirects to) for a
    Workday, Oracle Recruiting Cloud, iCIMS or SuccessFactors board.

    Returns {"platform", "method", "board_url", "evidence"} or None. None
    covers every non-answer — nothing found, the page named several
    boards or platforms, or the fetch failed. A failed fetch is not
    "in-house" (invariant 1), but it is also not an answer this function
    can give, so the caller keeps the research result unchanged.
    Pass html to reuse a page that is already in hand.
    """
    if not careers_url:
        return None
    corpus = []
    if html is None:
        try:
            resp = _guess_session.get(careers_url, headers=_SNIFF_UA, timeout=15)
        except requests.RequestException:
            return None
        if resp.status_code != 200:
            return None
        corpus.extend(r.headers.get("Location", "") for r in resp.history)
        corpus.append(resp.url)
        html = resp.text
    # JSON-embedded URLs arrive as https:\/\/host\/path or with \u002F for
    # the slashes — undo both so the patterns below see ordinary URLs.
    corpus.append(re.sub(r"\\/|\\u002f", "/", html, flags=re.I))
    text = "\n".join(corpus)

    boards = {}
    for platform, method, rx, build in _SNIFF_PATTERNS:
        # Keyed lowercase so case-only repeats count once, but the original
        # spelling is kept: a Workday site or an Oracle siteNumber
        # (CX_1001) is case-sensitive in the API path.
        found = {}
        for m in rx.finditer(text):
            found.setdefault(build(m).lower(), build(m))
        if found:
            boards[platform] = (method, found)
    if len(boards) != 1:
        return None
    platform, (method, found) = next(iter(boards.items()))
    if len(found) != 1:
        return None
    board = next(iter(found.values()))
    return {
        "platform": platform, "method": method,
        "board_url": board if platform != "SuccessFactors" or "company=" in board else careers_url,
        "evidence": f"{platform} reference found in the page source: {board}",
    }


def find_via_search(company_name, hq=None, known_platform=None):
    """Fallback: ask Claude (with web search) to fully profile a new
    company — sector/description, HQ, ATS platform, and careers URL — in
    one pass. Returns a dict:
      {
        "sector": str or None,
        "hq": str or None,
        "ats_platform": str,
        "scrape_method": str,
        "careers_url": str or None,
        "ambiguous": bool,
        "ambiguity_note": str,
      }

    If multiple distinct companies plausibly match this name, this does
    NOT guess — it sets ambiguous=True with a note describing the
    candidates found, and leaves sector/hq/ats as an "AMBIGUOUS" marker
    rather than silently attaching the wrong company's details. A wrong
    guess is worse than an honest "needs a human to clarify."

    known_platform: if the ATS is ALREADY confirmed (e.g. this call is only
    backfilling a missing Careers URL for a company we already know is on
    Greenhouse), pass the platform name here. This targets the search at
    that specific platform's job-board URL pattern instead of a generic
    "find their careers page" search — without this, a search often finds
    and saves the company's marketing homepage instead of the actual
    Greenhouse/Lever/Ashby link, which the scraper then can't parse a slug
    out of, causing scrapes to fail even though a URL was "found."
    """
    if not ANTHROPIC_API_KEY:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not set — cannot run the search fallback. "
        )

    hq_hint = f' The user has a hint that it may be headquartered in or near "{hq}".' if hq else ""

    platform_domains = {
        "Greenhouse": "boards.greenhouse.io or job-boards.greenhouse.io",
        "Lever": "jobs.lever.co",
        "Ashby": "jobs.ashbyhq.com",
        "Workday": "myworkdayjobs.com (a board like https://tenant.wd5.myworkdayjobs.com/SiteName)",
        "Oracle Recruiting Cloud": "oraclecloud.com (a board like https://x.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1)",
        "SuccessFactors": "successfactors.com or sapsf.com, or the company's own branded career-site domain",
        "iCIMS": "icims.com (a portal like https://careers-company.icims.com)",
        "Avature": "avature.net, or the company's own /careers/ domain built on Avature",
        "Taleo": "taleo.net (a section like https://company.taleo.net/careersection/)",
    }
    if known_platform and known_platform in platform_domains:
        url_instruction = f"""This company's ATS is ALREADY CONFIRMED as {known_platform}. Do not
re-research the ATS platform — your only job is to find their SPECIFIC
{known_platform} job board URL, which will contain the domain
{platform_domains[known_platform]}. Search specifically for
"{company_name} {known_platform} jobs" or similar, and report that exact
URL — not the company's general marketing homepage, which won't work for
scraping. If you cannot find the specific {known_platform}-hosted URL after
searching, report URL as "N/A" rather than substituting the homepage."""
    else:
        url_instruction = """Its careers page and which Applicant Tracking System (ATS) it uses
   (e.g. Greenhouse, Lever, Ashby, Workday, Workable, iCIMS, or "None" if
   jobs are listed directly on an in-house page, or "Not found" if there's
   no public careers page at all)."""

    prompt = f"""You're researching a company called "{company_name}" for a target list of
medtech/health-tech/digital-health startups a commercial exec is tracking
for potential roles. This can be adapted to any industry — edit this prompt
and the rubric in scoring/rubric.md to match your own target sector.{hq_hint}

Search the web and try to identify:
1. What the company does (one sentence, e.g. "AI-native home health provider").
2. Where it's headquartered (city, state/country).
3. {url_instruction}

IMPORTANT — check for name collisions: "{company_name}" might match more
than one real company (a common word, a name reused across industries, a
similarly-named but unrelated business). If your search turns up multiple
plausible, genuinely different companies and you can't confidently tell
which one the user means, DO NOT GUESS. Instead report it as ambiguous and
briefly describe the distinct candidates you found.

Respond with EXACTLY these lines and nothing else:
AMBIGUOUS: <true or false>
AMBIGUITY_NOTE: <if ambiguous, 1-2 sentences describing the candidate companies found, so a human can pick; otherwise empty>
SECTOR: <one-sentence description, or "AMBIGUOUS — see note" if ambiguous>
HQ: <city, state/country, or "AMBIGUOUS — see note" if ambiguous>
ATS: <platform name, or "None (in-house careers page)", or "Not found", or "AMBIGUOUS — see note" if ambiguous>
METHOD: <API, HTML, or Manual check>
URL: <the actual careers/jobs page URL you found, or "N/A" if there's no dedicated careers page>
HOMEPAGE: <the company's main website URL (e.g. "https://example.com"), or "N/A" if you couldn't find the company at all>"""

    resp = session.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-5",
            # 1500, not 500. With web search enabled the model narrates its
            # searching before emitting the structured block, and 500 was
            # not enough room for both — the block got cut off mid-reply,
            # which is the truncation the stop_reason check below catches.
            # Raising the cap makes that rare; the check makes it safe.
            "max_tokens": 1500,
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    if not resp.ok:
        # Surface Anthropic's actual error message (e.g. "credit balance too
        # low") instead of a generic "400 Bad Request" that hides the real
        # cause — this is what made a billing outage look like dozens of
        # unrelated search failures instead of one clear, fixable reason.
        try:
            detail = resp.json().get("error", {}).get("message", resp.text)
        except Exception:
            detail = resp.text
        raise RuntimeError(f"{resp.status_code} error from Anthropic API: {detail}")
    data = resp.json()

    # A reply that ran out of tokens before finishing is neither an error
    # nor a real answer, and it is the single most dangerous shape this
    # function can receive: resp.ok is True, the structured block is
    # missing or half-written, and every _extract() below quietly falls
    # through to its default — writing "Not found (auto-search failed)"
    # into Notion as a PERMANENT marker (invariant 8), which then also
    # excludes the company from the careers-URL backfill. The company
    # leaves the pipeline on the strength of a reply that never finished.
    # Raising instead puts it in the existing retry path (invariant 1).
    if data.get("stop_reason") == "max_tokens":
        raise RuntimeError(
            f"research reply for {company_name} hit the token cap before "
            f"finishing — treating as a failed search, not a 'not found'"
        )

    text = "".join(
        block["text"] for block in data.get("content", []) if block.get("type") == "text"
    )

    def _extract(label, default=""):
        """Pull one labelled line out of the reply.

        Two details, both of which produced wrong answers before:

        - **Anchored to line starts.** The old pattern searched anywhere in
          the joined text, so a label appearing mid-sentence in the model's
          own prose ("...their ATS: Greenhouse, probably...") could win
          over the real structured line, and a label that is a substring of
          another word could match by accident.
        - **Last match, not first.** With web search enabled the model
          often narrates before answering, and that narration can contain
          a line that looks like the block. The real block is the one it
          ends with, so later beats earlier.
        """
        matches = re.findall(rf"^[ \t]*{label}:[ \t]*(.*)$", text, re.M)
        return matches[-1].strip() if matches else default

    def _clean_url(raw):
        """The model sometimes appends explanatory commentary after the
        URL on the same line (e.g. "https://x.com/careers (jobs listed via
        https://jobs.ashbyhq.com/x)") — that full line was being saved
        verbatim as the URL, producing a broken, unfetchable string and
        sometimes pointing the scraper at the wrong ATS entirely.

        This extracts every URL token present and, when a known ATS
        domain appears anywhere (even in a parenthetical aside), prefers
        THAT one — the real, scrapeable job-board link — over a generic
        homepage/careers link that happens to appear first. Falls back
        to the first URL found if no known ATS domain is present."""
        if not raw:
            return raw
        all_urls = re.findall(r"https?://\S+", raw)
        if not all_urls:
            return raw

        known_ats_domains = ("greenhouse.io", "lever.co", "ashbyhq.com",
                             "myworkdayjobs.com", "oraclecloud.com", "icims.com",
                             "successfactors.com", "sapsf.com", "avature.net",
                             "taleo.net")
        for candidate in all_urls:
            if any(domain in candidate for domain in known_ats_domains):
                return candidate.rstrip(").,;\"'")

        return all_urls[0].rstrip(").,;\"'")

    # A reply carrying none of the three fields that are supposed to be
    # present unconditionally did not follow the format at all — a refusal,
    # a pure-prose answer, or a block mangled past recognition. Same
    # reasoning as the stop_reason check: defaulting here would write a
    # permanent "Not found" on the strength of a reply we never parsed.
    # _SENTINEL rather than "" so that a present-but-empty line (a real,
    # if unhelpful, answer) is still treated as an answer.
    _SENTINEL = object()
    if all(_extract(label, _SENTINEL) is _SENTINEL
           for label in ("AMBIGUOUS", "SECTOR", "ATS")):
        raise RuntimeError(
            f"research reply for {company_name} contained none of the "
            f"AMBIGUOUS/SECTOR/ATS lines the prompt asks for — treating as "
            f"a failed search, not a 'not found'"
        )

    ambiguous = _extract("AMBIGUOUS", "false").lower().startswith("true")
    url = _clean_url(_extract("URL", "N/A"))
    if url in ("N/A", ""):
        url = None

    homepage = _clean_url(_extract("HOMEPAGE", "N/A"))
    if homepage in ("N/A", ""):
        homepage = None

    # If there's no dedicated careers page but we do know the homepage,
    # use the homepage as a fallback scrape target — a lot of small
    # companies just link "Careers" from their nav or list roles right on
    # the homepage, so this at least gives the scraper something to try
    # instead of leaving the company permanently unreachable.
    careers_url = url or homepage

    return {
        "sector": _extract("SECTOR") or None,
        "hq": _extract("HQ") or None,
        "ats_platform": _extract("ATS", "Not found (auto-search failed)"),
        "scrape_method": _extract("METHOD", "Manual check"),
        "careers_url": careers_url,
        "used_homepage_fallback": careers_url is not None and careers_url == homepage and url is None,
        "ambiguous": ambiguous,
        "ambiguity_note": _extract("AMBIGUITY_NOTE"),
    }


def find_company_info(company_name, hq=None, known_platform=None):
    """Full pipeline: always search for a complete profile (sector, HQ,
    ATS, careers URL) — a new company needs all of that filled in, not
    just its ATS. The known-pattern check is still used, but only as a
    free, reliable double-check on the ATS platform specifically: if it
    confirms a platform, that overrides whatever the search step guessed,
    since a live API response is more trustworthy than a search result.

    known_platform: pass this when the ATS is already confirmed and only
    the Careers URL is missing — see find_via_search's docstring for why
    this matters (targets the search at the real platform-hosted URL
    instead of risking a generic homepage that can't be scraped).
    """
    info = find_via_search(company_name, hq, known_platform=known_platform)

    # An in-house / not-found answer is the one most worth double-checking:
    # a branded front end over Workday, Oracle, SuccessFactors or iCIMS reads
    # as in-house to a search and only shows in the page source.
    if (not info["ambiguous"] and info.get("careers_url")
            and ats_is_unresolved(info["ats_platform"])):
        sniffed = sniff_ats_from_page(info["careers_url"])
        if sniffed:
            info["ats_platform"] = sniffed["platform"]
            info["scrape_method"] = sniffed["method"]
            info["careers_url"] = sniffed["board_url"]
            info["sniffed_from_page"] = sniffed["evidence"]
            return info

    if not info["ambiguous"]:
        platform, method = try_known_patterns(company_name)
        if platform:
            info["ats_platform"] = platform
            info["scrape_method"] = method

    return info


def find_ats(company_name, hq=None):
    """Backward-compatible wrapper — returns just (ats_platform, scrape_method, careers_url)."""
    info = find_company_info(company_name, hq)
    return info["ats_platform"], info["scrape_method"], info["careers_url"]

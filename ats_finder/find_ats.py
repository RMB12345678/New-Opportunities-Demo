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

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

# (platform name, url template, scrape method) — tried in order
KNOWN_PATTERNS = [
    ("Greenhouse", "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", "API"),
    ("Lever", "https://api.lever.co/v0/postings/{slug}?mode=json", "API"),
    ("Ashby", "https://api.ashbyhq.com/posting-api/job-board/{slug}", "API"),
]


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
            resp = requests.get(url, timeout=6)
            if resp.status_code == 200 and resp.json():
                return platform, method
        except requests.RequestException:
            continue
    return None, None


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

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-5",
            "max_tokens": 500,
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

    text = "".join(
        block["text"] for block in data.get("content", []) if block.get("type") == "text"
    )

    def _extract(label, default=""):
        m = re.search(rf"{label}:\s*(.+)", text)
        return m.group(1).strip() if m else default

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

        known_ats_domains = ("greenhouse.io", "lever.co", "ashbyhq.com")
        for candidate in all_urls:
            if any(domain in candidate for domain in known_ats_domains):
                return candidate.rstrip(").,;\"'")

        return all_urls[0].rstrip(").,;\"'")

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

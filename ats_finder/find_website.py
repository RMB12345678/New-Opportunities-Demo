"""
Derives a company's public website domain — used to fill the Companies
`Website` property and to build a page-icon logo (Google's public favicon
endpoint) — from data the pipeline already has, cheapest option first.

Strategy:
1. If the saved Careers URL's own host is a real company domain (not a known
   ATS/aggregator/job-board/Notion host), that host IS the website. Free,
   and covers most companies, since a saved Careers URL usually already
   points at the company's own domain (see get_all_companies() in
   notion/client.py for how Careers URL itself gets backfilled).
2. Otherwise — careers URL missing, ATS-hosted (job-boards.greenhouse.io/x,
   jobs.ashbyhq.com/x, ...), a Google-search fallback, or just malformed —
   ask Claude (with web search) for the company's homepage domain. This is
   a single-purpose prompt, not the full sector/HQ/ATS profile lookup
   find_via_search() already does elsewhere: Website is normally being
   backfilled for companies whose profile is already fully researched, and
   re-running that whole prompt here would double-pay for it.

A company genuinely unresolvable by either path still gets a result: see
derive_website()'s docstring for why returning BLANK_ICON matters as much as
returning None.
"""
import re
from urllib.parse import urlparse

from ats_finder.find_ats import ANTHROPIC_API_KEY
from http_client import session

# Hosts that are an ATS, job board, or social/profile site, not a company's
# own domain. A Careers URL landing on one of these says nothing about
# where the company's real homepage lives — this is the same class of
# problem _clean_url() in find_ats.py solves for careers pages, just for
# "is this host the company itself" instead of "which URL is the real one".
NON_COMPANY_HOSTS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com",
    "workable.com", "personio.com", "personio.de", "paylocity.com",
    "breezy.hr", "comeet.com", "jobvite.com", "recruitee.com",
    "smartrecruiters.com", "icims.com", "bamboohr.com", "homerun.co",
    "notion.site", "notion.so", "google.com", "linkedin.com",
    "indeed.com", "glassdoor.com", "wellfound.com", "angel.co",
    "builtin.com", "ziprecruiter.com",
)

# Used as the page icon for a company where no real logo could be derived —
# a plain white square rather than Notion's default page icon, so an
# unresolved company reads as visually blank instead of looking identical to
# one nobody has looked at yet. That distinction is what lets
# get_companies_missing_website() in notion/client.py tell "already checked,
# genuinely nothing there" apart from "never processed" — see its docstring.
BLANK_ICON = "⬜"


def _real_company_host(url):
    """Return the bare host (no `www.`) if `url` looks like it points at the
    company's own domain, else None. None covers: no url, unparseable,
    no dot in the host (a bare word like "phoenixkinetics", or a full
    sentence saved by mistake), and any host in NON_COMPANY_HOSTS or a
    subdomain of one.
    """
    if not url:
        return None
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return None
    host = host.split("@")[-1].split(":")[0]  # strip userinfo@ / :port, if present
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host:
        return None
    if any(host == h or host.endswith("." + h) for h in NON_COMPANY_HOSTS):
        return None
    return host


def favicon_icon_url(host):
    """Google's public favicon endpoint for `host` — free, no key, no
    signup. (Clearbit's logo API, the obvious alternative, was sunset;
    Logo.dev is the modern replacement but needs an account.)
    """
    return f"https://www.google.com/s2/favicons?domain={host}&sz=128"


def find_website_homepage(company_name, hq=None):
    """Single-purpose Anthropic + web-search lookup for just a company's
    homepage domain.

    Returns the homepage URL, or None if the search genuinely found
    nothing / was ambiguous. Raises if the search itself failed to run
    (network error, API outage, billing issue) — that is NOT the same as a
    real "not found" answer (CLAUDE.md invariant 1), and the caller must
    not treat it as one.
    """
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set — cannot look up a homepage.")

    hq_hint = f' It is headquartered in or near "{hq}".' if hq else ""
    prompt = (
        f'What is the official homepage URL (not a careers/ATS page, not a '
        f'LinkedIn, Crunchbase, or job-board profile) of the company '
        f'"{company_name}"?{hq_hint} If more than one distinct real company '
        f'could plausibly match this name and you cannot tell which is '
        f'meant, reply with exactly "AMBIGUOUS". If you cannot find a real '
        f'company by this name at all, reply with exactly "NONE". '
        f'Otherwise reply with ONLY the homepage URL and nothing else.'
    )
    resp = session.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-sonnet-5",
            "max_tokens": 200,
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=60,
    )
    if not resp.ok:
        # Same reasoning as find_via_search(): surface Anthropic's real
        # error instead of a generic "400 Bad Request" that hides it.
        try:
            detail = resp.json().get("error", {}).get("message", resp.text)
        except Exception:
            detail = resp.text
        raise RuntimeError(f"{resp.status_code} error from Anthropic API: {detail}")

    data = resp.json()
    text = "".join(
        block["text"] for block in data.get("content", []) if block.get("type") == "text"
    ).strip()

    if not text or text.upper() in ("NONE", "AMBIGUOUS"):
        return None

    urls = re.findall(r"https?://\S+", text)
    raw = urls[0].rstrip(").,;\"'") if urls else text
    return raw


def derive_website(careers_url, company_name, hq=None):
    """Best-effort (website_url, icon) for one company.

    website_url is None when nothing could be confirmed. icon is ALWAYS
    set — a real favicon on success, BLANK_ICON on a genuine "nothing
    found" — because the icon is what marks this row as processed at all;
    see get_companies_missing_website() in notion/client.py. Only an
    exception (a failed lookup, not a completed one) skips writing
    anything, so a transient failure gets retried next run instead of
    being mistaken for a checked-and-empty answer.
    """
    host = _real_company_host(careers_url)
    if host:
        return f"https://{host}", favicon_icon_url(host)

    # No usable host in the saved Careers URL — ask instead. Lets an
    # exception here (a failed search) propagate uncaught, same contract
    # as find_via_search()/find_company_info().
    homepage = find_website_homepage(company_name, hq=hq)
    homepage_host = _real_company_host(homepage)
    if homepage_host:
        return f"https://{homepage_host}", favicon_icon_url(homepage_host)

    return None, BLANK_ICON

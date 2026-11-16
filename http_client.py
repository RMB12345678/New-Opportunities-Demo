"""Shared HTTP session with automatic retry/backoff for transient failures
(rate limits, gateway timeouts) — used by notion/client.py, scoring/score_jobs.py,
ats_finder/find_ats.py's Anthropic call, and all four scrapers.

Retries only the status codes actually worth retrying — a 429 (rate
limited) or a 5xx (transient server issue) can succeed on the next attempt
seconds later; a 400 or 401 never will. `allowed_methods` is required, not
optional: urllib3's Retry does not retry POST/PATCH by default (treated as
non-idempotent), which would silently defeat retries on every Notion write
and every Anthropic call.

This is the "patient" session — five attempts with growing backoff, never
sleeping on a server-supplied Retry-After (punch list #28). Code
that fires deliberately speculative requests it expects to fail (see
ats_finder/find_ats.py's try_known_patterns) should use its own
fast-fail session instead, not this one.
"""
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_retry = Retry(
    total=5,
    backoff_factor=1.5,
    status_forcelist=[429, 500, 502, 503, 504, 529],
    allowed_methods=["GET", "POST", "PATCH"],
    # False: Retry.sleep_for_retry obeys a server's Retry-After, and the
    # request timeout does not cover that sleep. A vendor answering 429/503
    # with a huge value froze a run for ~3 hours on the third-party session
    # (invariant 25); every scraper, Notion and Anthropic share this one, so
    # only our own bounded backoff_factor may decide how long to wait.
    # Punch list #28.
    respect_retry_after_header=False,
)

session = requests.Session()
session.mount("https://", HTTPAdapter(max_retries=_retry))
session.mount("http://", HTTPAdapter(max_retries=_retry))

def response_text(resp):
    """resp.text, decoded sensibly when the server declared no charset.

    requests falls back to ISO-8859-1 for text/* with no charset, which turns
    UTF-8 punctuation in a real job title into mojibake ("PA \u00c2\u00b7
    Full-time"). When the Content-Type names a charset it is trusted; when it
    does not, the detected one is used instead."""
    content_type = (getattr(resp, "headers", None) or {}).get("content-type", "") or ""
    if "charset" not in content_type.lower():
        detected = getattr(resp, "apparent_encoding", None)
        if detected:
            resp.encoding = detected
    return resp.text


# For Notion page CREATES only. The patient session above retries POST on
# 502/504 and on read timeouts, and a create is not idempotent: if Notion
# committed the page before the gateway answered with an error, urllib3 re-sends
# it and a second row exists for the same URL, which invariant 20 says no later
# run can ever reach. So this one retries only what provably did not reach
# Notion: a 429 (rejected before processing) and connect errors (never
# connected). read=0 stops a read timeout, where the outcome is unknown, from
# being retried. A create that fails is reconciled by URL on the next run.
_create_retry = Retry(
    total=5,
    connect=5,
    read=0,
    other=0,
    backoff_factor=1.5,
    status_forcelist=[429],
    allowed_methods=["POST"],
    respect_retry_after_header=False,   # same reasoning as the patient session
)

create_session = requests.Session()
create_session.mount("https://", HTTPAdapter(max_retries=_create_retry))
create_session.mount("http://", HTTPAdapter(max_retries=_create_retry))

# For fetching arbitrary company websites (ats_finder/find_embedded_board.py,
# scrapers/html_generic.py), not an API we have a relationship with. The patient
# session above honours Retry-After, and a site answering 429/503 with a huge
# value makes urllib3 sleep for that long inside Retry.sleep_for_retry — a sleep
# the request timeout does not cover. One froze a run for ~3 hours; Sonivie
# answers 503 with Retry-After: 600. respect_retry_after_header=False means only
# our own backoff_factor decides how long to wait (invariant 25).
_third_party_retry = Retry(
    total=2,
    backoff_factor=1,
    status_forcelist=[429, 502, 503, 504],
    allowed_methods=["GET"],
    respect_retry_after_header=False,
)

third_party_session = requests.Session()
third_party_session.mount("https://", HTTPAdapter(max_retries=_third_party_retry))
third_party_session.mount("http://", HTTPAdapter(max_retries=_third_party_retry))

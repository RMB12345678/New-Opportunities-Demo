"""Shared HTTP session with automatic retry/backoff for transient failures
(rate limits, gateway timeouts) — used by notion/client.py, scoring/score_jobs.py,
ats_finder/find_ats.py's Anthropic call, and all four scrapers.

Retries only the status codes actually worth retrying — a 429 (rate
limited) or a 5xx (transient server issue) can succeed on the next attempt
seconds later; a 400 or 401 never will. `allowed_methods` is required, not
optional: urllib3's Retry does not retry POST/PATCH by default (treated as
non-idempotent), which would silently defeat retries on every Notion write
and every Anthropic call.

This is the "patient" session — five attempts with growing backoff. Code
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
    respect_retry_after_header=True,
)

session = requests.Session()
session.mount("https://", HTTPAdapter(max_retries=_retry))
session.mount("http://", HTTPAdapter(max_retries=_retry))

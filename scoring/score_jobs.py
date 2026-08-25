"""
Scores job postings against the fit rubric (scoring/rubric.md), using the
Anthropic API. Returns a score 1-5, short reasoning, an ambiguity flag if
applicable, and which Notion/Excel view it should route to, matching the
rubric's own routing rules.

Cost-saving measures, all transparent to the caller:
  - Uses Haiku (cheapest current model) rather than Sonnet — this is a
    rubric-following/classification task, not one that needs a larger
    model's extra reasoning.
  - Batches multiple jobs into a single API call instead of one call per
    job, so the ~5,300-character rubric text is only sent once per batch
    rather than once per job.
  - Marks the rubric text as cacheable (prompt caching), so repeated
    calls in the same run are billed at a steep discount for that
    unchanged portion.
  - Persists scored results to output/score_cache.json, keyed by job URL,
    so a re-run only scores genuinely new postings rather than
    re-scoring everything from scratch every time. Each entry is tagged
    with a fingerprint of the rubric/model/keyword-list/prompt shape that
    produced it (see RUBRIC_FINGERPRINT below); changing any of those
    invalidates the affected entries instead of silently serving stale
    scores.

Requires env var ANTHROPIC_API_KEY.
"""
import os
import re
import json
import html
import hashlib

from http_client import session

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
MODEL = "claude-haiku-4-5-20251001"
# Batches of 5 rather than 15, now that each entry carries posting text
# instead of just a title: attention degrades across more/longer items, so
# index misalignment between the response array and the batch gets more
# likely as batches grow. And when a batch does fail to parse, the fallback
# re-sends every job in it one at a time — losing 5 jobs of input to a
# retry is cheaper than losing 15.
BATCH_SIZE = 5
CACHE_PATH = os.path.join(os.path.dirname(__file__), "..", "output", "score_cache.json")

_EXCLUDED_KEYWORDS_PATH = os.path.join(os.path.dirname(__file__), "excluded_title_keywords.json")
with open(_EXCLUDED_KEYWORDS_PATH) as f:
    EXCLUDED_TITLE_KEYWORDS = json.load(f)

_RUBRIC_PATH = os.path.join(os.path.dirname(__file__), "rubric.md")
with open(_RUBRIC_PATH) as f:
    RUBRIC_TEXT = f.read()

# Bump whenever the prompt or listing shape changes in a way that could
# change a job's score, even if RUBRIC_TEXT itself didn't move — e.g. the
# addition of posting-text to the listing below. "2" because "1" was the
# title-only prompt this fingerprinting scheme was introduced to replace.
SCORER_VERSION = "2"

SYSTEM_PROMPT = f"""You are scoring job postings for fit against the rubric below.
Follow it exactly, including the ambiguity-flagging and routing rules.

{RUBRIC_TEXT}

You will be given a numbered list of job postings in one message. Each
entry includes the company, job title, location, and the posting's own
text where available. If a posting's text is "not available", do not
infer the rubric's substantive tests (revenue band, clinical-sales
environment, "15+ years", keyword checks, etc.) from the title alone —
score it as best you can from what's given, but set "ambiguous": true
and use "ambiguity_note" to say the score is based on title and company
only, no posting text. Missing evidence is uncertainty, not a low score.

Respond with ONLY a JSON array, no other text, with one object per posting IN THE
SAME ORDER, in exactly this shape:
{{
  "index": <the posting's number>,
  "score": <integer 1-5, or 0 for Non-fit per the rubric>,
  "reasoning": "<2-3 sentences, per the rubric>",
  "ambiguous": <true or false>,
  "ambiguity_note": "<the 'Ambiguous: could also score X if Y applies' line, or empty string if not ambiguous>",
  "ic_role_flag": <true or false — true if this is an individual contributor role>,
  "routing": "<one of: 'Scored', 'Needs review', 'Non-fit'>"
}}"""


def _load_cache():
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH) as f:
            return json.load(f)
    return {}


def _save_cache(cache):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2)


def _job_key(job):
    # URL is unique per posting; fall back to company+title if URL is missing.
    return job.get("url") or f"{job.get('company')}::{job.get('title')}"


def _prompt_fingerprint():
    """A cache entry is only a real answer to "how does this job score
    under the CURRENT rubric" if nothing that could change the score has
    moved since it was written — the rubric text, the model, the excluded-
    keyword list, or the prompt/listing shape itself (SCORER_VERSION).
    Entries keyed on job URL alone silently kept serving pre-change scores
    forever; this fingerprint is compared against each entry's "rubric_fp"
    in score_all() so a change forces a re-score instead."""
    payload = (RUBRIC_TEXT + MODEL
               + json.dumps(EXCLUDED_TITLE_KEYWORDS, sort_keys=True)
               + SCORER_VERSION)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


RUBRIC_FINGERPRINT = _prompt_fingerprint()


def _write_cache_entry(cache, key, result):
    """Write a scored result to the cache, tagged with the fingerprint of
    the rubric/prompt that produced it. If this overwrites an entry that
    was scored under a *different* fingerprint but still holds a real
    score, keep that old entry nested under "previous" rather than
    discarding it — old score next to new is the raw material for a future
    outcome-feedback loop, and it costs nothing to keep."""
    result["rubric_fp"] = RUBRIC_FINGERPRINT
    old = cache.get(key)
    if old is not None and old.get("score") is not None and old.get("rubric_fp") != RUBRIC_FINGERPRINT:
        result["previous"] = {k: v for k, v in old.items() if k != "previous"}
    cache[key] = result


def _clean_description(raw):
    """Normalize a scraper's raw description field into plain text short
    enough to put in a prompt. Greenhouse's `content` comes back HTML-
    escaped HTML (e.g. "&lt;div&gt;...&lt;/div&gt;"); Lever/Ashby's
    descriptionPlain is already plain text but still carries the original
    HTML's whitespace. unescape + strip tags handles both in one pass —
    tag-stripping a string with no tags is a no-op.

    Truncation lives here, not in the scrapers, because 2500 chars is
    coupled to BATCH_SIZE and max_tokens (300 * batch size) below: a
    scraper has no way to know what the batch math downstream needs."""
    if not raw:
        return None
    text = html.unescape(raw)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:2500] if text else None


def _score_batch(batch):
    """Score up to BATCH_SIZE jobs in a single API call."""
    listing = "\n\n".join(
        f"[{i}]\nCompany: {j.get('company')}\n"
        f"Company description: {j.get('company_description') or 'unknown'}\n"
        f"Job title: {j.get('title')}\n"
        f"Location: {j.get('location') or 'unknown'}\n"
        f"--- Posting text ---\n{_clean_description(j.get('description')) or 'not available'}\n--- end posting ---"
        for i, j in enumerate(batch)
    )

    resp = session.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 300 * len(batch),
            "system": [
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": listing}],
        },
        timeout=120,
    )
    if not resp.ok:
        # Surface Anthropic's actual error message, not just "400 Bad Request" —
        # the response body explains what was actually wrong with the request.
        try:
            detail = resp.json().get("error", {}).get("message", resp.text)
        except Exception:
            detail = resp.text
        raise RuntimeError(f"{resp.status_code} error from Anthropic API: {detail}")

    data = resp.json()

    text = "".join(
        block["text"] for block in data.get("content", []) if block.get("type") == "text"
    )
    return _parse_json_array(text, expected=len(batch))


def _score_batch_with_fallback(batch):
    """Try scoring the whole batch in one call. A parse failure (the model
    didn't return the expected JSON array) is isolated to one bad posting
    in the batch more often than not, so that case falls back to scoring
    each job individually to find the culprit without losing the other N-1
    good results.

    Any OTHER failure (an HTTP error the shared session's own retries
    couldn't fix — a 429/529 that stayed down through 5 attempts, or a
    non-retryable error) is NOT retried per-job: the session already tried
    the retryable cases, so resending the same request N more times would
    just repeat the same failure at N times the cost."""
    try:
        return _score_batch(batch)
    except _BatchParseError as parse_error:
        print(f"     [warn] batch of {len(batch)} failed to parse ({parse_error}); "
              f"retrying each job individually to isolate the problem")
        results = []
        for job in batch:
            try:
                single_result = _score_batch([job])
                results.append(single_result[0])
            except Exception as single_error:
                print(f"     [fail] '{job.get('title')}' at {job.get('company')}: {single_error}")
                results.append(_failure_result(str(single_error)))
        return results
    except Exception as batch_error:
        print(f"     [fail] batch of {len(batch)} failed after retries ({batch_error}); "
              f"not retrying per-job — a systemic/rate-limit failure won't be "
              f"fixed by resending the same requests")
        return [_failure_result(str(batch_error)) for _ in batch]


def _enforce_routing(result):
    """The model returns both a score and a routing label, but nothing
    guarantees it kept them consistent with the rubric's own rule:
    ambiguous -> Needs review, score 0 -> Non-fit, everything else -> Scored.
    Enforce that here rather than trusting the model's routing field as-is."""
    if result.get("ambiguous"):
        result["routing"] = "Needs review"
    elif result.get("score") == 0:
        result["routing"] = "Non-fit"
    elif result.get("score") is not None:
        result["routing"] = "Scored"
    # if score is None (a genuine scoring failure), leave routing as-is —
    # _failure_result already sets it to "Needs review".
    return result


def _matches_excluded_keyword(job):
    """Free, instant check — no API call. If the job title contains any
    word/phrase from excluded_title_keywords.json, it's auto-routed to
    Non-fit without ever being sent to the model. Case-insensitive,
    substring match (so "engineer" also catches "Sales Engineer" and
    "VP of Engineering" — broader than just IC roles, worth knowing if
    that list ever needs narrowing to something like "\\bengineer\\b" as a
    whole-word-only match instead)."""
    title = (job.get("title") or "").lower()
    for keyword in EXCLUDED_TITLE_KEYWORDS:
        if keyword.lower() in title:
            return keyword
    return None


def score_all(jobs, dry_run=False):
    """Score a list of jobs, using the cache to skip anything already
    scored in a previous run. Returns the same list with score/reasoning/
    routing fields added.

    dry_run=True skips the Anthropic call entirely (and never writes
    output/score_cache.json) — it only reports what WOULD be scored."""
    if not dry_run and not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set — cannot score jobs.")

    cache = _load_cache()

    # Re-apply the routing rule to anything already in the cache too, in
    # case it was scored before this rule existed (or by an earlier,
    # buggier version) and is sitting there inconsistent.
    for key, result in cache.items():
        _enforce_routing(result)

    # Free pre-filter: jobs matching an excluded title keyword get an
    # instant Non-fit, written straight to cache, no API call at all. Skips
    # a job only if it's cached AND current under today's fingerprint —
    # otherwise a rubric/keyword-list change would never re-run this check.
    keyword_filtered = 0
    for job in jobs:
        key = _job_key(job)
        cached = cache.get(key)
        if cached and cached.get("score") is not None and cached.get("rubric_fp") == RUBRIC_FINGERPRINT:
            continue  # already scored (or already excluded) under the current rubric
        matched_keyword = _matches_excluded_keyword(job)
        if matched_keyword:
            _write_cache_entry(cache, key, {
                "score": 0,
                "reasoning": f"Auto-filtered: title contains excluded keyword '{matched_keyword}'.",
                "ambiguous": False,
                "ambiguity_note": "",
                "ic_role_flag": False,
                "routing": "Non-fit",
            })
            keyword_filtered += 1
    if keyword_filtered:
        if not dry_run:
            _save_cache(cache)
        print(f"[score] {keyword_filtered} job(s) auto-filtered to Non-fit by title keyword, no API call"
              + (" (dry run — not saved to cache)" if dry_run else ""))

    # A cached entry only counts as "already scored" if it has a score AND
    # was scored under today's rubric fingerprint. A previous failure
    # (score is None) is not a real answer — it should be retried, not
    # treated as permanently resolved. A fingerprint mismatch means the
    # rubric, model, keyword list, or prompt shape moved since this entry
    # was written, so it's serving a stale answer, not a current one.
    # Without either check, a bad run or a rubric edit would leave jobs
    # stuck on an old verdict forever.
    def _is_stale(entry):
        if entry is None or entry.get("score") is None:
            return True
        return entry.get("rubric_fp") != RUBRIC_FINGERPRINT

    to_score = [j for j in jobs if _is_stale(cache.get(_job_key(j)))]
    previously_failed = 0
    stale_fingerprint = 0
    for j in to_score:
        entry = cache.get(_job_key(j))
        if entry is None:
            continue
        if entry.get("score") is None:
            previously_failed += 1
        elif entry.get("rubric_fp") != RUBRIC_FINGERPRINT:
            stale_fingerprint += 1
    already_scored = len(jobs) - len(to_score)

    print(f"[score] rubric fingerprint: {RUBRIC_FINGERPRINT}")
    if stale_fingerprint:
        print(f"[score] rubric/prompt changed since these were last scored — "
              f"{stale_fingerprint} job(s) will be re-scored")
    print(f"[score] {already_scored} already scored (cached), {len(to_score)} to score"
          + (f" ({previously_failed} retries of a previous failure, "
             f"{stale_fingerprint} re-scores from a rubric change)"
             if (previously_failed or stale_fingerprint) else ""))

    if dry_run:
        if to_score:
            print(f"[score] [dry-run] would call Anthropic to score {len(to_score)} job(s) — skipping")
    else:
        for start in range(0, len(to_score), BATCH_SIZE):
            batch = to_score[start:start + BATCH_SIZE]
            results = _score_batch_with_fallback(batch)

            for job, result in zip(batch, results):
                _enforce_routing(result)
                _write_cache_entry(cache, _job_key(job), result)
            _save_cache(cache)  # save after every batch, not just at the end

    scored = []
    for job in jobs:
        result = cache.get(_job_key(job))
        job = dict(job)
        if result is not None:
            job.update({k: v for k, v in result.items() if k != "index"})
        elif not dry_run:
            job.update({k: v for k, v in _failure_result("not found in cache").items() if k != "index"})
        # dry_run with no cached result: leave the job without score fields —
        # claiming "Scoring failed" here would misrepresent a deliberate skip
        # as a real failure that never happened.
        scored.append(job)
    return scored


def _failure_result(error_msg):
    return {
        "score": None,
        "reasoning": f"Scoring failed: {error_msg}",
        "ambiguous": True,
        "ambiguity_note": "Automatic scoring failed — needs manual review.",
        "ic_role_flag": False,
        "routing": "Needs review",
    }


class _BatchParseError(Exception):
    """The model's response wasn't the expected JSON array — a generation
    problem isolated to this specific batch/prompt, not an HTTP/network
    failure. Raised (rather than silently returning failure placeholders)
    so _score_batch_with_fallback can tell this apart from a 429/529 the
    shared session already retried and gave up on — only THIS failure mode
    is worth splitting into per-job calls to isolate."""
    pass


def _parse_json_array(text, expected):
    cleaned = re.sub(r"^```json\s*|\s*```$", "", text.strip())
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, list) and len(parsed) == expected:
            return sorted(parsed, key=lambda r: r.get("index", 0))
    except json.JSONDecodeError:
        pass
    # Parsing failed or count mismatch — fail the whole batch safely rather
    # than risk misaligning results to the wrong jobs.
    raise _BatchParseError(f"could not parse batch response: {text[:150]}")


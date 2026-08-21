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
    re-scoring everything from scratch every time.

Requires env var ANTHROPIC_API_KEY.
"""
import os
import re
import json
import requests

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
MODEL = "claude-haiku-4-5-20251001"
BATCH_SIZE = 15
CACHE_PATH = os.path.join(os.path.dirname(__file__), "..", "output", "score_cache.json")

_EXCLUDED_KEYWORDS_PATH = os.path.join(os.path.dirname(__file__), "excluded_title_keywords.json")
with open(_EXCLUDED_KEYWORDS_PATH) as f:
    EXCLUDED_TITLE_KEYWORDS = json.load(f)

_RUBRIC_PATH = os.path.join(os.path.dirname(__file__), "rubric.md")
with open(_RUBRIC_PATH) as f:
    RUBRIC_TEXT = f.read()

SYSTEM_PROMPT = f"""You are scoring job postings for fit against the rubric below.
Follow it exactly, including the ambiguity-flagging and routing rules.

{RUBRIC_TEXT}

You will be given a numbered list of job postings in one message. Respond
with ONLY a JSON array, no other text, with one object per posting IN THE
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


def _score_batch(batch):
    """Score up to BATCH_SIZE jobs in a single API call."""
    listing = "\n\n".join(
        f"[{i}]\nCompany: {j.get('company')}\n"
        f"Company description: {j.get('company_description') or 'unknown'}\n"
        f"Job title: {j.get('title')}\n"
        f"Location: {j.get('location') or 'unknown'}"
        for i, j in enumerate(batch)
    )

    resp = requests.post(
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
    """Try scoring the whole batch in one call. If that fails, fall back
    to scoring each job in the batch individually — this isolates whether
    the problem is one bad job (common) or something systemic (rare), and
    means one problematic posting doesn't take out 14 good ones with it."""
    try:
        return _score_batch(batch)
    except Exception as batch_error:
        print(f"     [warn] batch of {len(batch)} failed ({batch_error}); "
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


def score_all(jobs):
    """Score a list of jobs, using the cache to skip anything already
    scored in a previous run. Returns the same list with score/reasoning/
    routing fields added."""
    if not ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY not set — cannot score jobs.")

    cache = _load_cache()

    # Re-apply the routing rule to anything already in the cache too, in
    # case it was scored before this rule existed (or by an earlier,
    # buggier version) and is sitting there inconsistent.
    for key, result in cache.items():
        _enforce_routing(result)

    # Free pre-filter: jobs matching an excluded title keyword get an
    # instant Non-fit, written straight to cache, no API call at all.
    keyword_filtered = 0
    for job in jobs:
        key = _job_key(job)
        if key in cache:
            continue  # already scored (or already excluded) in a past run
        matched_keyword = _matches_excluded_keyword(job)
        if matched_keyword:
            cache[key] = {
                "score": 0,
                "reasoning": f"Auto-filtered: title contains excluded keyword '{matched_keyword}'.",
                "ambiguous": False,
                "ambiguity_note": "",
                "ic_role_flag": False,
                "routing": "Non-fit",
            }
            keyword_filtered += 1
    if keyword_filtered:
        _save_cache(cache)
        print(f"[score] {keyword_filtered} job(s) auto-filtered to Non-fit by title keyword, no API call")

    # A cached entry only counts as "already scored" if it actually has a
    # score. A previous failure (score is None) is not a real answer — it
    # should be retried, not treated as permanently resolved. Without this
    # check, a bad run (e.g. a transient API outage) would leave every job
    # from that run stuck as "failed" forever, since the cache would keep
    # skipping them on every future run too.
    to_score = [j for j in jobs if cache.get(_job_key(j), {}).get("score") is None]
    previously_failed = sum(
        1 for j in to_score if _job_key(j) in cache and cache[_job_key(j)].get("score") is None
    )
    already_scored = len(jobs) - len(to_score)
    print(f"[score] {already_scored} already scored (cached), {len(to_score)} to score"
          + (f" ({previously_failed} of those are retries of a previous failure)" if previously_failed else ""))

    for start in range(0, len(to_score), BATCH_SIZE):
        batch = to_score[start:start + BATCH_SIZE]
        results = _score_batch_with_fallback(batch)

        for job, result in zip(batch, results):
            _enforce_routing(result)
            cache[_job_key(job)] = result
        _save_cache(cache)  # save after every batch, not just at the end

    scored = []
    for job in jobs:
        result = cache.get(_job_key(job), _failure_result("not found in cache"))
        job = dict(job)
        job.update({k: v for k, v in result.items() if k != "index"})
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
    return [_failure_result(f"could not parse batch response: {text[:150]}") for _ in range(expected)]


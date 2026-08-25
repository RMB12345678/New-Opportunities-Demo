# Job Agent Punch List

19 improvements, ordered by how much each changes the quality of what lands in the
Notion dashboard. Verified against commit `243b2f2` on 2026-08-22. #18 and #19 were
found 2026-08-23 during a live `--dry-run` and added after the original 17.

**As of 2026-08-24: 5 done, 1 resolved as intentional, 2 partial, 11 open.**
Each item carries its own status line with what actually landed.

Severity is damage to the output. Effort is a rough estimate for a Claude-assisted
change. Suggested batching is at the bottom, and it is **not** the same as the
priority order, because several items depend on another landing first.

| # | Issue | Severity | Effort | Status |
|---|---|---|---|---|
| 1 | Scorer never sees the job description | Critical | 2–3 h | Done |
| 2 | A failed scrape marks live jobs as closed | Critical | 45 m | Done |
| 3 | A truncated research reply becomes a permanent "not found" | Critical | 30 m | Open |
| 4 | Score cache ignores rubric changes | Critical | 30 m | Done |
| 5 | HTML scraper collapses a company's jobs into one URL | High | 1–2 h | Open |
| 6 | Two rubric copies, already out of sync | High | 30 m | Open |
| 7 | No retry or rate-limit handling anywhere | High | 2 h | Done (partial) |
| 8 | Prompt caching probably isn't firing | High | 45 m | Open |
| 9 | Two sources of truth for "new this run" | High | 30 m | Partial |
| 10 | Placeholder text leaks into the scoring prompt | Medium | 20 m | Open |
| 11 | Jobs without a URL get paid for, then dropped | Medium | 30 m | Open |
| 12 | Four full Notion table scans per run | Medium | 1 h | Open |
| 13 | Scraping is fully sequential | Medium | 1–2 h | Open |
| 14 | Scheduling will silently destroy both caches | Medium | 2–3 h | Open |
| 15 | Keyword filter is substring-based and cached forever | Medium | 30 m | Resolved |
| 16 | Scores of 1 and 2 sit in the main list | Low | 20 m | Open |
| 17 | No run summary, no cost visibility, no tests | Low | 2–3 h | Partial |
| 18 | Windows console encoding crashes the run on unrenderable characters | High | 5 m | Done |
| 19 | Embedded ATS boards are undetectable | Critical | 1–2 h | Open |

---

## 1. The scorer never sees the job description
**Critical** · `scoring/score_jobs.py`, `scrapers/*.py`

**Status: done, 2026-08-23** (commit `94cb425`). All five fix steps landed.
The three API scrapers return the raw description field (`content` for
Greenhouse, `descriptionPlain` for Lever and Ashby, verified against live
responses); `html_generic.py` sets it to `None` explicitly. Cleaning and the
2500-character truncation happen in `_clean_description()` next to
`_score_batch()`, so the limit sits beside the batch-size and token maths it
has to stay consistent with. `BATCH_SIZE` dropped 15 → 5. `SYSTEM_PROMPT` now
tells the model to flag a posting whose text is "not available" as ambiguous
with an explanatory note, rather than inferring the rubric's substantive tests
from a title — that routes those to Needs Review through the existing
`_enforce_routing()` rule instead of burying them in Non-fits.

`_score_batch()` sends four lines per posting: company, company description, job
title, location. Every substantive test in the rubric needs the posting body —
revenue band, clinical-sales environment, post-merger integration, "15+ years", and
the entire "Keywords to check for in posting text" section. None of that is visible
from a title, so the model infers it. `scrapers/greenhouse.py` already requests
`?content=true` and then discards the field.

**Fix**
1. Add a `description` key to the normalized job dict in all four scrapers.
   Greenhouse: `job["content"]` (HTML-escaped, needs unescaping). Lever:
   `job["descriptionPlain"]`. Ashby: `job["descriptionPlain"]`. Verify the Lever and
   Ashby field names against a live response first. HTML generic: leave `None`.
2. Strip tags, collapse whitespace, truncate to ~2500 chars.
3. Add it to the listing in `_score_batch()` with explicit delimiters:
   ```
   f"--- Posting text ---\n{(j.get('description') or 'not available')[:2500]}\n--- end posting ---"
   ```
4. Drop `BATCH_SIZE` from 15 to 5. `max_tokens` is `300 * len(batch)`; a long batch
   truncates, fails JSON parsing, and falls back to 15 individual calls at full price.
5. Tell the model in the prompt to say so when posting text is missing rather than
   inferring.

**Depends on #4.** Without cache fingerprinting, every already-scored job keeps its
title-only score forever.

## 2. A failed scrape marks that company's live jobs as closed
**Critical** · `notion/client.py` → `sync_jobs_to_notion()`, `run.py` → `run_scrapers()`

**Status: done, 2026-08-24.** All five fix steps landed. `run_scrapers()`
returns a third value, `scraped_ok` — the set of company names whose
scraper returned without raising (zero jobs counts as success; companies
with no matching scraper never enter the set, since never-attempted proves
as little as failed). `_query_all_job_postings()` takes the Target List
lookup and resolves each row's Company relation to a name, matching on
relation page id rather than name strings; the Target List query moved
ahead of it and is passed in rather than re-fetched, so this adds no fourth
table scan (#12). The close pass classifies before it executes: company
scraped OK closes, company that failed is left open and counted, and a row
whose company is absent or unresolvable is left open and counted
*separately* — unknown provenance means we can't confirm the scrape
succeeded, and defaulting to close is the original bug.

The safety valve aborts the entire close pass if a run would close more
than 25% of currently-open rows, and never a partial subset. It carries a
20-row floor (`CLOSE_VALVE_MIN_OPEN_ROWS`): a bare ratio deadlocks a small
database, where one genuine closure out of three rows is 33% and would
abort identically on every subsequent run, so that row could never close.
Under the floor the ratio is skipped and a `[valve]` line says so, which is
what makes an abort in the log distinguishable from a spurious one.

Under `--dry-run` the close list prints one line per candidate row with its
company and whether that company scraped this run. Covered by
`tests/test_close_pass.py` (11 checks, fully stubbed — no network, no cost).

**Production evidence the bug had already fired:** the 2026-08-24 dry run
found **Sword Health** holding just 2 open rows in Notion while its scraper
returned 28 live postings. Sword Health scrapes fine — it is not in the
failed set — so those ~26 rows were closed by an earlier run that hit a
transient failure on that company, exactly the mechanism described above.
The other 11 companies in that run's close list were all within normal churn
(deltas of −3 to +3 against their prior open-row counts), which is what
makes Sword Health stand out rather than look like ordinary turnover.

**How much of that damage is permanent — corrected 2026-08-24.** An earlier
draft of this entry claimed those rows could never come back on their own.
That is wrong. `sync_jobs_to_notion` sets `"Still Open": "__YES__"` on
*every* scraped job, and a wrongly-closed row whose URL turns up again takes
the update branch, so **Still Open is restored automatically** by the next
run that scrapes that company successfully. No recovery pass is needed for
it, and none should be written.

What does *not* recover is `New This Run`: the URL is already in Notion, so
`is_new_in_notion` is false and the checkbox stays off. The row returns to
the Scored List quietly, without ever reappearing in the New This Run view.
So the lasting cost of this bug is not lost rows — it is roles that silently
skipped the one view meant to surface them, for however many runs passed
before the company scraped cleanly again. That is a review-workflow gap, not
a data-loss one, and it is worth knowing which roles it hit before trusting
that view's history.

The close loop treats absence from this run as proof the posting is gone. But
`run_scrapers()` catches every scrape exception into `skipped`, so a timeout, a
transient 500, or a renamed slug means that company contributes zero URLs and all its
open roles get `Still Open = false`. They vanish from the active views permanently:
the next successful run finds their URLs already in Notion, treats them as updates,
and leaves "New This Run" off.

**Fix**
1. `run_scrapers()` returns a third value: the set of company names that scraped
   successfully. Zero jobs counts as success; raising does not.
2. `sync_jobs_to_notion(jobs, scraped_ok)`.
3. `_query_all_job_postings()` also reads each row's Company relation and resolves it
   to a name. Only close a row when its company is in `scraped_ok`.
4. Log what was deliberately left open: `"14 job(s) from 3 failed companies left open"`.
5. Safety valve: if a run would close more than 25% of all open rows, abort the close
   pass and warn. That is a systemic failure, not 40 roles filled on one Tuesday.

## 3. A truncated research reply becomes a permanent "not found"
**Critical** · `ats_finder/find_ats.py` → `find_via_search()`

`max_tokens: 500` with web search enabled, and nothing checks `stop_reason`. If the
model gets cut off before emitting its structured block, `resp.ok` is still `True`,
`_extract()` falls through to its defaults, and `"Not found (auto-search failed)"` is
written to Notion as a permanent marker.

This is exactly the case the retry-safety design exists to prevent. A truncated reply
is neither an error nor a real "found nothing", and it lands in the permanent bucket.
Worse, `get_companies_missing_url()` excludes any company whose ATS contains "Not
found", so the company also loses its careers-URL backfill and leaves the pipeline.

The status doc counts ~90 companies with no ATS found. Some share may be truncation.

**Fix**
1. Check the stop reason and raise, so it lands in the existing retry path:
   ```python
   data = resp.json()
   if data.get("stop_reason") == "max_tokens":
       raise RuntimeError(
           f"research reply for {company_name} hit the token cap before "
           f"finishing — treating as a failed search, not a 'not found'"
       )
   ```
2. Raise `max_tokens` from 500 to 1500.
3. If `AMBIGUOUS`, `SECTOR`, and `ATS` are all absent from the text, the reply didn't
   follow the format. Raise instead of defaulting.
4. Same function, separate bug: `_extract()` runs `re.search` over all text blocks
   joined, first match wins. With web search on, the model can emit prose first.
   Anchor to line starts and take the last match:
   `re.findall(rf"^{label}:\s*(.+)$", text, re.M)[-1]`
5. Recovery: clear ATS Platform on every row reading `"Not found (auto-search failed)"`
   so the next run redoes them. `reset_pe_vc_firms.py` is the template — same
   operation, different marker string. Run this **after #7**.

## 4. The score cache ignores rubric changes
**Critical** · `scoring/score_jobs.py` → `_job_key()`, `score_all()`

**Status: done, 2026-08-23** (commit `94cb425`). `_prompt_fingerprint()`
hashes the rubric text, the model id, the excluded-keyword list, and a new
`SCORER_VERSION` constant. Every cache write goes through
`_write_cache_entry()`, which stamps `rubric_fp` and, when overwriting a
stale-but-real score, nests the old entry under `"previous"` rather than
discarding it. `score_all()` treats a fingerprint mismatch exactly like a
missing score and reports how many jobs are being re-scored for that reason
specifically. First application invalidated all 1,099 cached entries, since
none carried the key.

Entries are keyed on job URL alone. Nothing records which rubric produced the score.
Adding "Head of Product" on Aug 17 changed nothing for anything already cached.

**Fix**
```python
import hashlib

SCORER_VERSION = "1"  # bump when the prompt shape changes

def _prompt_fingerprint():
    payload = (RUBRIC_TEXT + MODEL
               + json.dumps(EXCLUDED_TITLE_KEYWORDS, sort_keys=True)
               + SCORER_VERSION)
    return hashlib.sha256(payload.encode()).hexdigest()[:12]
```
1. Store `"rubric_fp"` on every entry when written.
2. In `score_all()`, treat a mismatched fingerprint as unscored, like `score is None`.
3. Print the consequence before spending: `"rubric changed — 812 job(s) will be re-scored"`.
4. Keep the stale entry rather than deleting it. Old score next to new is the raw
   material for the outcome-feedback loop.

## 5. The HTML scraper collapses a company's jobs into one URL
**High** · `scrapers/html_generic.py`

Affects the ~90 companies with no real ATS.

- `"url": link if link else careers_url` — every job whose anchor had no usable href
  gets the careers page URL. They share a cache key, share a Notion dedup key, and
  collapse into one row.
- The `OVERRIDES` path uses `el.get("href") if el.name == "a" else None`, which
  returns `None` for any selector targeting an `h3`/`span`/`div`. So every override
  company reports all jobs under one URL.
- `if href.startswith("/")` skips `"careers/vp-commercial"` and `"./jobs/123"`, which
  get stored as broken relative strings.
- `seen` holds lowercased titles, so one title in two locations keeps only the first.
- A saved ATS URL can also go stale when a company's board is deleted or renamed
  (confirmed: CMR Surgical's saved `boards.greenhouse.io/cmrsurgical` 404s) — don't
  build embed detection for this, see #2's failure-tracking/logging instead.

**Fix**
1. Move `from urllib.parse import urljoin` to module scope, call it unconditionally on
   any non-empty, non-`#`, non-`mailto:` href.
2. In the OVERRIDES branch, try `el.find_parent("a")` and `el.find("a")` before giving up.
3. Drop jobs with no usable link and count them: `"Acme: 6 jobs, 2 skipped (no link)"`.
4. Dedup on resolved URL, not title.
5. Optional, biggest coverage win: a Playwright fallback for JavaScript-rendered pages.

## 6. Two rubric copies, already drifted
**High** · `scoring/rubric.md`, Claude Project doc "Scoring Rubric"

`scoring/rubric.md` lists **Head of Product**. The Project doc does not. The code reads
the file, so the file wins today, but reasoning from the Project copy will mislead.

**Fix**
1. Simplest: make `scoring/rubric.md` the only copy; replace the Project doc body with
   a pointer plus a clearly-labelled read-only snapshot.
2. Better, once scheduled: fetch the rubric from Notion at run start, write it to
   `scoring/rubric.md`, score from that. Then editing it in Notion changes behavior.
3. Either way, print the fingerprint from #4 at the top of each run.

## 7. No retry or rate-limit handling anywhere
**High** · `notion/client.py`, `scoring/score_jobs.py`, `scrapers/*.py`

**Status: done, 2026-08-23.** `http_client.py` holds the one shared session
described in fix step 1 below (with `allowed_methods` — required, since
`urllib3.Retry` doesn't retry POST/PATCH by default). It's used in
`notion/client.py` (reads and writes), `scoring/score_jobs.py`,
`ats_finder/find_ats.py`'s `find_via_search`, and all four scrapers.
`try_known_patterns` deliberately does **not** use it — it fires
speculative guessed-slug requests that are supposed to 404, so it gets its
own fast-fail session (`total=1`) instead of paying the shared session's
backoff on every wrong guess. The old hand-rolled `_write_with_retry`
(retried on bare `Exception`, so a permanent 400/401 still cost ~6s before
failing) is deleted. Fix step 2 (throttle) is done — 0.35s after each
Notion write in `_create_job_posting`/`_update_job_posting`. Fix step 3 is
done — `_score_batch_with_fallback` now only splits into per-job calls on a
`_BatchParseError`; any other failure (429/529 that survived the shared
session's own retries) fails the whole batch once instead of re-issuing the
same doomed request per job. Fix step 4 (resumable sync) is **not** done —
still open.

Every HTTP call is a bare `requests.get`/`post` with a timeout and no retry. Notion
averages ~3 req/s before 429, and `sync_jobs_to_notion()` fires one unthrottled write
per job plus full pagination on two databases. The sync runs *after* scoring, so a 429
raises having already spent the money, leaving Notion half-written with no resume point.

**Fix**
1. One shared session in a small `http.py`:
   ```python
   from requests.adapters import HTTPAdapter
   from urllib3.util.retry import Retry

   retry = Retry(total=5, backoff_factor=1.5,
                 status_forcelist=[429, 500, 502, 503, 504, 529],
                 allowed_methods=["GET", "POST", "PATCH"],
                 respect_retry_after_header=True)
   session = requests.Session()
   session.mount("https://", HTTPAdapter(max_retries=retry))
   ```
2. Throttle Notion writes (~0.35 s between calls).
3. `_score_batch_with_fallback` should only split into per-job calls on a *parse*
   error. On 429/529, back off and retry the whole batch.
4. Make the sync resumable: append synced URLs to a run-scoped file, skip them on restart.

## 8. Prompt caching probably isn't firing
**High** · `scoring/score_jobs.py` → `_score_batch()`

`cache_control: ephemeral` is set, but Haiku's minimum cacheable prefix is 2048 tokens
and the system prompt is roughly 1500–1700. Below the floor the marker is ignored
silently. Nothing reads the `usage` block, so there is no way to tell.

**Fix**
1. Log `usage`: `input_tokens`, `output_tokens`, `cache_creation_input_tokens`,
   `cache_read_input_tokens`. If the last two are always 0, caching isn't happening.
   **Confirm before optimizing.**
2. If it is the floor: either drop the marker, or pad the cached prefix past 2048 with
   3–4 annotated scoring examples (a 5, a 3, a 0). Those improve calibration *and*
   cross the threshold.
3. Print a per-run token and estimated-cost summary.

## 9. Two sources of truth for "new this run"
**High** · `scoring/track_new_postings.py`, `notion/client.py`, `output_excel.py`

**Partial, 2026-08-24.** The `--dry-run` leak in `mark_new_postings()` is
fixed (it takes a `dry_run` parameter and skips `_save_seen()`), so a dry run
no longer mutates run history. The substantive item — removing the second
source of truth entirely — is still open.

`sync_jobs_to_notion` computes `is_new_in_notion` from Notion's own rows. `output_excel.py`
builds its "New This Run" tab from `j.get("is_new")`, which comes from `seen_jobs.json`.
The two artifacts of one run disagree.

**Fix**
1. Have `sync_jobs_to_notion` write its verdict back: `job["is_new"] = is_new_in_notion`.
2. `main()` already syncs before writing `jobs.json` and calling `build_workbook()`,
   which re-reads that file — so the mutation propagates. Confirm the order holds.
3. Delete `scoring/track_new_postings.py` and `seen_jobs.json`.

## 10. Placeholder marker text leaks into the scoring prompt
**Medium** · `run.py` → `run_scrapers()`

`job["company_description"] = row.get("sector")` copies Sector / Focus verbatim,
including markers like `"Not found (checked, no info located — clear this field to
retry)"` and `"AMBIGUOUS — see console output from this run for candidates"`.

**Fix**
```python
_MARKER_PREFIXES = ("Not found", "AMBIGUOUS", "N/A")

def clean_sector(value):
    if not value or value.strip().startswith(_MARKER_PREFIXES):
        return None
    return value
```
The prompt already renders `None` as `'unknown'`, which is the right signal.

## 11. Jobs without a URL get scored, then dropped
**Medium** · `scoring/score_jobs.py`, `notion/client.py`

`_job_key()` falls back to `"{company}::{title}"`, so URL-less jobs get scored and
cached. Then `sync_jobs_to_notion` hits `if not url: continue` and drops them.

**Fix**
1. Skip URL-less jobs *before* scoring, and count them.
2. #5 makes most of these disappear. What remains is genuinely unlinkable and belongs
   on the "Needs Manual Check" tab.
3. Alternative: dedup on `company + title` when URL is missing, write the careers page
   as the URL, flag it in the ATS column as `"HTML (no direct link)"`.

## 12. Four full Notion table scans per run
**Medium** · `notion/client.py`, `run.py` → `main()`

All three `get_companies_missing_*` helpers call `get_all_companies()` internally, and
`main()` calls it once more. 4 × 4 paginated requests over unchanged data. It is also
a correctness issue: the snapshots are taken at different moments while earlier steps
are writing back.

**Fix**
1. Fetch once in `main()`, pass the list down.
2. Turn the `get_companies_missing_*` functions into pure filters taking a list.
3. Keep the one re-pull before scraping. 4 scans → 2.

Same file: `NOTION_API_KEY = os.environ["NOTION_API_KEY"]` at module scope throws a
bare `KeyError` on import. Use `.get()` plus a message naming `.env`, as
`score_jobs.py` already does.

## 13. Scraping is fully sequential
**Medium** · `run.py`

331 companies one at a time, each a round trip with a 10–15 s timeout, plus
`time.sleep(1)` after every backfill write.

**Fix**
1. `ThreadPoolExecutor(max_workers=8)` around the scrape loop. Pure I/O against
   different hosts.
2. Collect `(row, jobs, error)` tuples; print and bookkeep in the main thread after
   the pool drains.
3. Leave Notion writes sequential and throttled.
4. Cut the `try_known_patterns` timeout from 6 s to ~3 s.

## 14. Scheduling will silently destroy both caches
**Medium** · `.gitignore`, `output/`, planned GitHub Actions

`.gitignore` excludes `output/*.json`. An Actions runner starts clean every time, so
every scheduled run re-scores everything from scratch. That disables the cost controls
exactly when run frequency goes up.

**Fix** — pick one before the first scheduled run:
1. `actions/cache` keyed on the fingerprint from #4. Least code, but GitHub evicts
   after 7 days of no access, which a weekly schedule sits right on.
2. Commit the cache: move to `state/score_cache.json`, un-ignore, workflow commits
   back. Durable and diffable; watch file size.
3. Make Notion the cache: Score, Reasoning, Routing and URL are already stored there.
   Read existing scores at run start, skip anything already scored under the current
   fingerprint. Removes local state entirely. Best long-term answer.

Also: keys in repository secrets, a concurrency group so two runs can't sync Notion at
once, and make the workflow fail loudly.

## 15. Keyword filter is substring-based and cached permanently
**Medium** · `scoring/score_jobs.py`, `scoring/excluded_title_keywords.json`

**Status: resolved as intentional, 2026-08-23.** Substring matching is
deliberate, not a bug: the exclusion list is meant to catch every title
containing the word, management roles included. The genuine risk in this item
was cache permanence — a keyword exclusion written as `score: 0` would survive
a change to the list. #4's fingerprint hashes `EXCLUDED_TITLE_KEYWORDS`, so
editing the list now invalidates the whole cache and everything is
re-evaluated. No code change needed.

List is currently `["territory manager", "engineer"]`, so blast radius is small today.
`"engineer"` as a substring also kills `"VP Engineering & Commercial Operations"`.
Exclusions are written to the cache as `score: 0` with no marker distinguishing them
from real model scores, so removing a keyword doesn't revive what it killed.

**Fix**
1. Whole-word matching: `re.search(rf"\b{re.escape(keyword)}\b", title, re.I)`.
2. Tag entries `"source": "keyword_filter"` and invalidate on keyword-list hash change
   (already covered by #4's fingerprint).
3. Print what was filtered: `"12 filtered: 'engineer' × 9, 'territory manager' × 3"`.

## 16. Scores of 1 and 2 sit in the main Scored List
**Low** · `scoring/rubric.md`, `scoring/score_jobs.py` → `_enforce_routing()`

The code matches the rubric's routing summary, but the rubric also defines 1 and 2 as
"deal-breaker present". Those are roles already decided against, in the daily list.
A rubric decision, not a bug.

**Fix**
1. Add a `"Low fit"` routing value for scores 1–2.
2. Update `_enforce_routing()`, the rubric's routing summary (both copies until #6),
   the Notion Routing select options, and the Excel tabs together.
3. The rubric header says "score 1-5" but the guide defines a 0. Say 0–5.

## 17. No run summary, no cost visibility, no tests
**Low** · project-wide

**Partial, 2026-08-24.** A `tests/` directory now exists, covering the #2
close-pass logic: relation resolution, the `scraped_ok` guard, the 25% ratio,
the 20-row floor, and the empty-database case. Logging, the run summary, the
geo pre-filter, and coverage of the other pure functions are still open.

- Everything reports through `print()`. Fine at a terminal, useless on a schedule.
- No structured run record: companies scraped, jobs found/scored, tokens spent, rows
  created/closed, failures by reason.
- No geographic filter — every posting worldwide gets scored.
- No tests. The trickiest logic is pure and easy to cover: `extract_slug`,
  `resolve_scraper`, `_parse_json_array`, `_enforce_routing`, `_clean_url`. Every one
  has been the site of a real bug.
- Stale docs: `find_ats.py`'s docstring claims seven ATS platforms, `KNOWN_PATTERNS`
  has three. `output_excel.py` says "Two tabs", builds five.
- Inconsistent model pinning: scoring pins `claude-haiku-4-5-20251001`, research uses
  the floating alias `claude-sonnet-5`. Pin both.

**Fix**
1. Swap `print` for `logging` with timestamps. Keep the message text.
2. Write `output/run_summary.json` each run, print a 6-line digest.
3. `tests/` with pytest over the five pure functions, using fixture strings from
   postings that actually broke. ~100 lines.
4. Geo pre-filter next to the keyword filter: configurable location allowlist, applied
   before the API call, tagged in the cache so it can be undone.
5. After #14, an email digest of new 4s and 5s is a small addition on top of
   `run_summary.json`.

## 18. Windows console encoding crashes the run on unrenderable characters
**High** · `run.py`

**Status: done, 2026-08-23.**

Python 3.14 on Windows defaults `stdout`/`stderr` to the system codepage
(cp1252), not UTF-8. Any job title, company name, or model reasoning
containing a curly quote, en dash, ™, or accented character raises
`UnicodeEncodeError` the moment a `print()` tries to render it — killing the
run mid-pipeline, potentially after real Notion writes and Anthropic spend
already happened for that batch. Found live: a `--dry-run` scrape hit a
title containing `→` and crashed before reaching the scoring step.

**Fix**
1. At the very top of `run.py`, before any other imports (right after the
   module docstring, so the docstring itself still works):
   ```python
   import sys
   sys.stdout.reconfigure(encoding="utf-8", errors="replace")
   sys.stderr.reconfigure(encoding="utf-8", errors="replace")
   ```
2. `errors="replace"` means an unrenderable glyph prints as a placeholder
   instead of crashing — the run keeps going instead of dying on a cosmetic
   console limitation.

## 19. Embedded ATS boards are undetectable
**Critical** · `run.py` → `extract_slug()`, `SLUG_URL_PATTERNS`

When a company's saved Careers URL is on its own domain (not a
`boards.greenhouse.io`/`jobs.lever.co`/`jobs.ashbyhq.com` URL), `extract_slug()`
has nothing to match against `SLUG_URL_PATTERNS` and returns `None`.
`run_scrapers()` then falls back to guessing a slug from the company name,
which 404s for any company whose real ATS token doesn't match its name —
exactly the class of bug #6 in this same file already fixed for the
*saved-URL* case ("guessing was why verified companies 404'd every run").
This is the same failure mode one layer earlier: a company embeds its
Greenhouse/Lever/Ashby board directly in its own careers page instead of
linking out to it, so there's no ATS-domain URL to save in the first
place, and today nothing ever looks at the embedding page's HTML to find
the real token.

Not yet confirmed against a specific company — see below.

**Fix**
1. When a company's saved Careers URL is on its own domain (i.e.
   `extract_slug()` returns `None`) and no slug was found any other way,
   fetch that page and search the raw HTML for an embed reference:
   `boards.greenhouse.io/embed/job_board?for=<slug>`, `jobs.lever.co/<slug>`,
   or `jobs.ashbyhq.com/<slug>`, in both `<script>`/`<iframe>` src
   attributes and inline JS.
2. If found, save the extracted slug back to the ATS-appropriate scraper
   path (update `ats_platform`/`scrape_method` if they don't already say
   Greenhouse/Lever/Ashby, since a company can look HTML-only today purely
   because its embed was never detected).
3. This only catches embeds present in the raw HTML. A page whose job
   board loads via client-side JavaScript (no embed string anywhere in the
   initial response) needs a headless-browser fetch instead — same
   limitation `html_generic.py`'s docstring already calls out, and a
   separate fix (a Playwright fallback is listed under #5).

**Investigation note:** CMR Surgical was the company that prompted this
item (its saved Careers URL is already `boards.greenhouse.io/cmrsurgical`,
which 404s, and the guessed-from-name slug 404s too — see #6's slug-guessing
problem). But fetching CMR Surgical's actual careers pages
(`www.cmrsurgical.com/careers`, `us.cmrsurgical.com/careers`, both 200 with
a browser User-Agent) and searching the raw HTML found no Greenhouse/Lever/
Ashby/Workday/iCIMS reference of any kind — no iframe, no script src, no
embed string. That looks more like a JS-rendered listing (fix step 3
above) than an embed sitting undetected in static HTML, so CMR Surgical
should NOT be treated as a confirmed example of this specific bug until
someone re-checks it in an actual browser (dev tools / rendered DOM) to
see what ATS, if any, it's really using. The embed-detection gap described
above is still real and worth fixing on its own merits — the site that
motivated writing it down just doesn't hold up as verified evidence for it.

---

## Suggested batching

One branch and one PR per batch. This order is not the priority order.

| Batch | Items | Why together |
|---|---|---|
| 1 | **#4, then #1** | Fingerprint first, so the description change actually forces a re-score. #1 alone leaves every existing job frozen on its title-only score. |
| 2 | **#7, then #2 and #3** | Retries first. #2 and #3 are the same problem in two places: telling a transient failure apart from a real answer. Run #3's recovery pass at the end. |
| 3 | **#6, #9, #10, #12, #16** | All small, independent, cleanup. One sitting. |
| 4 | **#14, #13, #17** | Cache persistence, run time, observability. Do these *before* the first scheduled run. |
| 5 | **#5, #8, #11, #15** | #5 is the biggest coverage win left and the most open-ended. |

Batch 1 will trigger a full re-score. Back up `output/score_cache.json` first and
budget for it.

# Job Agent Punch List

50 improvements, ordered by how much each changes the quality of what lands in the
Notion dashboard. Verified against commit `243b2f2` on 2026-08-22. #18 and #19 were
found 2026-08-23 during a live `--dry-run`, #20 on 2026-09-09 while measuring a
real run, and #23 on 2026-09-29 by auditing the Job Postings database itself, all
added after the original 17. #29-#51 (there is no #40; it was removed on 2026-11-16) came from a read-only code audit on 2026-11-16
(commit `48d7891`); they are numbered in the audit's severity order and have their
own three batches (A, B, C) at the bottom.

**As of 2026-11-16: 10 done, 1 resolved as intentional, 2 left as is by decision (#50, #51), 4 partial, 33 open.** #24-#27 are Large MedTech follow-ups; #28 came from the 2026-10-26 hang; #29-#51 are the 2026-11-16 audit. #1-#28 have not been re-audited since 2026-10-26, so check an item's own status line before trusting this count.
Each item carries its own status line with what actually landed.

Severity is damage to the output. Effort is a rough estimate for a Claude-assisted
change. Suggested batching is at the bottom, and it is **not** the same as the
priority order, because several items depend on another landing first.

| # | Issue | Severity | Effort | Status |
|---|---|---|---|---|
| 1 | Scorer never sees the job description | Critical | 2–3 h | Done |
| 2 | A failed scrape marks live jobs as closed | Critical | 45 m | Done |
| 3 | A truncated research reply becomes a permanent "not found" | Critical | 30 m | Done (recovery pending) |
| 4 | Score cache ignores rubric changes | Critical | 30 m | Done |
| 5 | HTML scraper collapses a company's jobs into one URL | High | 1–2 h | Done (step 5 open) |
| 6 | Two rubric copies, already out of sync | High | 30 m | Done |
| 7 | No retry or rate-limit handling anywhere | High | 2 h | Done (partial) |
| 8 | Prompt caching probably isn't firing | High | 45 m | Partial |
| 9 | Two sources of truth for "new this run" | High | 30 m | Partial |
| 10 | Placeholder text leaks into the scoring prompt | Medium | 20 m | Done |
| 11 | Jobs without a URL get paid for, then dropped | Medium | 30 m | Done |
| 12 | Four full Notion table scans per run | Medium | 1 h | Done |
| 13 | Scraping is fully sequential | Medium | 1–2 h | Open |
| 14 | Scheduling will silently destroy both caches | Medium | 2–3 h | Open |
| 15 | Keyword filter is substring-based and cached forever | Medium | 30 m | Resolved |
| 16 | Scores of 1 and 2 sit in the main list | Low | 20 m | Open |
| 17 | No run summary, no cost visibility, no tests | Low | 2–3 h | Partial |
| 18 | Windows console encoding crashes the run on unrenderable characters | High | 5 m | Done |
| 19 | Embedded ATS boards are undetectable | Critical | 1–2 h | Done |
| 20 | Notion sync rewrites every row every run | Critical | 3 h | Done |
| 21 | Named ATS platforms with no scraper | High | 3-4 h | Done |
| 22 | A wrong saved value is indistinguishable from a researched one | High | 1-2 h | Partial |
| 23 | Duplicate rows stay New This Run forever | High | 1 h | Done |
| 24 | Large MedTech companies no scraper reaches | High | 3-4 h | Open |
| 25 | JS-rendered SuccessFactors sites need a browser | Medium | 3 h | Open |
| 26 | SENIOR_TITLE_RE lets noise through | Low | 20 m | Open |
| 27 | resolve_scraper reads platform names out of negative text | Medium | 30 m | Done |
| 28 | Scrapers can still sleep on a server's Retry-After | High | 1 h | Done |
| 29 | A failed embedded board falls through to HTML and closes the company's rows | High | 20 m | Done |
| 30 | SmartRecruiters postings change URL at the 100-detail cap | High | 30 m | Done |
| 31 | score_cache.json is rewritten non-atomically after every batch | High | 20 m | Done |
| 32 | A guessed slug is accepted as the company's board | High | 1–2 h | Done (steps 1–2) |
| 33 | A failed score overwrites Routing, Reasoning and IC Role | Medium | 30 m | Done |
| 34 | Row creation is transparently retried and can duplicate | Medium | 30 m | Done |
| 35 | scraped_ok and the Company relation are keyed by name | Medium | 1 h | Done |
| 36 | Five scrapers read a missing key as an empty board | Medium | 30 m | Done |
| 37 | Oracle and SuccessFactors always scrape senior terms only | Medium | 30 m | Done |
| 38 | An interrupted sync loses New This Run on rows it created | Medium | 45 m | Done |
| 39 | Scorer output is not validated before use | Medium | 30 m | Done |
| 41 | rubric.md is read with the Windows default encoding | Medium | 5 m | Done |
| 42 | Scrape Note comparison can never be equal for connection errors | Low | 20 m | Done |
| 43 | Personio host fallback changes URL identity | Low | 20 m | Done |
| 44 | Scheme-less URLs from research loop or blank-stamp | Low | 20 m | Done |
| 45 | HTML scraper mojibake and unjoined relative links | Low | 30 m | Done |
| 46 | sniff_ats_from_page honours Retry-After on third-party sites | Low | 15 m | Done |
| 47 | Hardcoded Job Postings fallback id looks stale | Low | 5 m | Done |
| 48 | A blank-named Companies row costs a paid search every run | Low | 10 m | Done |
| 49 | Senior search terms lack "vp" and "svp" | Low | 5 m | Done |
| 50 | Unanimity is not ownership in board detection | Low | decision | Left as is |
| 51 | --dry-run overwrites jobs.json and the workbook | Low | 15 m | Left as is |

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

**Status: code done 2026-09-21; step 5's recovery pass deliberately not run
yet.** Steps 1-4 landed. A `stop_reason` of `max_tokens` now raises before
the text is parsed at all, `max_tokens` went 500 → 1500, a reply carrying
none of AMBIGUOUS/SECTOR/ATS raises, and `_extract()` anchors labels to line
starts and takes the LAST match rather than the first. That last one was not
theoretical: on a reply mentioning "ATS: something in my notes" before the
real block, the old pattern returned "something in my notes."

Step 5's script exists as `reset_failed_ats_research.py` (with `--dry-run`)
and is safe to run only now that 1-4 have landed — against the old code it
would re-create the same 36 markers at full price. Running it costs ~36
Anthropic research calls, so it is left as a deliberate decision rather than
bundled into the fix. `tests/test_research_reply.py` covers all four.

`max_tokens: 500` with web search enabled, and nothing checks `stop_reason`. If the
model gets cut off before emitting its structured block, `resp.ok` is still `True`,
`_extract()` falls through to its defaults, and `"Not found (auto-search failed)"` is
written to Notion as a permanent marker.

This is exactly the case the retry-safety design exists to prevent. A truncated reply
is neither an error nor a real "found nothing", and it lands in the permanent bucket.
Worse, `get_companies_missing_url()` excludes any company whose ATS contains "Not
found", so the company also loses its careers-URL backfill and leaves the pipeline.

The status doc counts ~90 companies with no ATS found. Some share may be truncation.

**Measured 2026-09-21** (while auditing ATS coverage for #21): **36** rows read
`"Not found (auto-search failed)"` exactly — that is step 5's recovery target, and
every one of them is a company a truncated reply may have written off. More
broadly, **162 of 470** rows carry a `"Not found"` ATS value of some kind, and all
162 are excluded from the careers-URL backfill by the rule quoted above. That is a
third of the database sitting outside the one pass that could repair it, which is
what makes step 5 worth more than its size suggests. #22 widened the same
selector's *other* condition and deliberately did not touch this one.

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
   so the next run redoes them. The shape is a small one-off script that finds every
   row carrying the marker, clears the field, and stops — no scraping, no scoring.
   Run this **after #7**.

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

**Status: partly done, 2026-11-16 (branch `fix/remaining-items`).** Fix steps 1-4 landed (batch C already did the `urljoin`/non-link part of step 1): `urljoin` on every href; the OVERRIDES branch tries the element, `find_parent("a")` then `find("a")`; a job with no usable link is skipped and counted (`Acme: 6 jobs, 2 skipped (no link)`) instead of borrowing the careers URL; dedup is on the resolved URL, not the title. If titles were found but **none** has a usable link the scraper raises `ScrapeShapeError` rather than returning `[]` (invariant 23). **Step 5 (Playwright for JS-rendered pages) is not done**: it needs a new dependency, which CLAUDE.md says to ask about. Companies that previously collapsed to one row at the careers URL will now lose that row (closed) and gain real ones, or fail visibly. Tests: `test_linkless_titles_are_skipped_...`, `test_titles_with_no_usable_link_at_all_raise_...`, `test_dedup_is_on_url_not_title`, `test_override_selector_finds_the_anchor_...`.

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

**Status: done, 2026-09-09.** Fix option 1 as landed: `scoring/rubric.md` is the
authoritative copy and the only one the code reads, and the Claude Project doc is now a
stamped read-only mirror of it rather than a second editable original. The drift that
motivated this item is gone — the Project copy has been synced, so Head of Product is
no longer missing from it. The convention going forward is to edit the repo copy and
re-stamp the Project copy from it; a Project-side edit is not a rubric change, because
nothing reads it.

Option 2 (fetch the rubric from Notion at run start, so editing it in Notion changes
behavior) was **not** done and is not queued. It only becomes worth the complexity once
runs are scheduled and nobody is at a checkout to edit the file — and it would make the
rubric a network dependency of every run, which #4's fingerprint would then have to
absorb on the fly. Revisit with #14.

Option 3 is already covered elsewhere: `score_all()` prints
`[score] rubric fingerprint: <hash>` at step 7, before it spends anything, and says how
many jobs a fingerprint change is about to re-score (that came with #4). #20 then passes
the same value into the sync so a mass write can be explained rather than merely
observed. It prints at step 7 rather than at the very top of the run, which is early
enough for both purposes.

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
1. One shared session in a small `http_client.py`:
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

**Status: partly done, 2026-11-16 (branch `fix/remaining-items`).** Steps 1 and 3 landed: each response's `usage` is summed (`score_jobs.USAGE`), `usage_report()` prints tokens, an estimated cost and whether caching is firing at the end of `score_all()`, and the run digest carries the estimate. **Step 2 (pad the cached prefix with scoring examples) is deliberately NOT done: it changes the system prompt, so it changes the fingerprint and re-scores every cached job.** Read the first real run's `[score]` line first: if it says the cache is not firing, step 2 is a separate, paid decision. The price constants in `PRICE_PER_MTOK` are an estimate to check against current pricing. Tests: `test_usage_is_summed_from_each_response`, `test_report_says_when_caching_is_not_firing`, `test_cost_estimate_weights_cache_reads_and_writes`.

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

**Status: partly done, 2026-11-16 (branch `fix/remaining-items`).** Steps 1-2 landed: `sync_jobs_to_notion()` writes `job["is_new"] = is_new_in_notion` onto the job as it goes (the same list `main()` writes to `jobs.json` and `build_workbook()` reads), so the Excel New This Run tab now agrees with the Notion view. **Step 3 (delete `track_new_postings.py` and `seen_jobs.json`) is not done**: `seen_jobs.json` is run history CLAUDE.md says not to delete casually, and `mark_new_postings()` still supplies `first_seen` and the step-6 count. Test: `test_sync_writes_is_new_back_onto_the_jobs`.

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

**Status: done, 2026-11-16 (branch `fix/remaining-items`).** `run.clean_sector()` returns `None` for "Not found...", "AMBIGUOUS..." and "N/A" markers (case-insensitive prefix), used by `prepare_jobs()`. Only the copy onto jobs changes; the Notion field keeps its marker (invariant 8). Cached scores are keyed by URL and are **not** re-scored by this; only postings scored from now on see `unknown`. Tests: `test_clean_sector_drops_markers_and_keeps_descriptions`, `test_prepare_jobs_uses_the_cleaned_sector`.

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

**Status: done, 2026-11-16 (branch `fix/remaining-items`).** Fix step 1: `drop_urlless_jobs()` runs right after `dedupe_jobs_by_url()` in `main()`, before scoring, and prints how many were dropped and from which companies. Steps 2-3 were not needed after #5's change. Tests: `test_urlless_jobs_are_dropped_and_counted`, `test_nothing_is_reported_when_every_job_has_a_url`.

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

**Status: done, 2026-11-16 (branch `fix/remaining-items`).** The four `get_companies_missing_*` selectors are pure filters over an optional `companies` list (`_rows()`), and the four `fill_missing_*` passes accept it. `main()` now reads Companies **twice**: once for steps 1-3, once after step 3 for step 4 and the scrape (see the comment there for why a stale first snapshot is safe: those steps only select rows lacking something the earlier one fills). One behaviour difference: a row whose step-1 research found an ATS but no Careers URL now gets its URL looked up on the next run rather than the same run. `notion/client.py` also raises a `RuntimeError` naming `.env` instead of a bare `KeyError` for a missing key. Tests: `test_selectors_do_not_rescan_notion_when_given_a_list`, `test_selectors_still_read_notion_when_given_nothing`, `test_missing_env_var_names_the_env_file_and_not_the_value`.

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
2. Update `_enforce_routing()`, the rubric's routing summary (the repo copy; the
   Project mirror gets re-stamped from it — see #6), the Notion Routing select
   options, and the Excel tabs together.
3. The rubric header says "score 1-5" but the guide defines a 0. Say 0–5.

## 17. No run summary, no cost visibility, no tests
**Low** · project-wide

**Status: partly done, 2026-11-16 (branch `fix/remaining-items`).** Added this round: `output/run_summary.json` (not written on a dry run) and a printed run digest (`build_run_summary()`, `print_run_digest()`), scoring token/cost reporting (#8), the stale `find_ats.py` and `output_excel.py` docstrings corrected. Pure-function tests now exist for `_parse_json_array`/`_enforce_routing` (batch C) and `extract_slug`/`resolve_scraper`/`_clean_url` (earlier). **Still open:** the `logging` swap (touches every print), the geographic pre-filter (changes what gets scored: a policy decision), and pinning the research model's floating alias. Test: `test_run_summary_has_the_fields_a_scheduled_run_needs`.

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
**Critical** · `run.py` → `run_scrapers()`, `ats_finder/find_embedded_board.py`

**Status: done, 2026-09-21.** Confirmed first, then built — the item was
written as unconfirmed, and the investigation note below was right that CMR
Surgical is not an example of it.

Sweeping all 198 HTML-marked companies that have a Careers URL found **20**
reaching a real ATS board that nothing was reading, in three shapes: the
saved URL redirects to the board (8), the page embeds it via script/iframe
or inline JS (7), or the page links to it (2, plus 3 mixed). Scraped through
`run_scrapers()` those 20 return **379 postings**. 10 of the 20 have a slug
the name-guess gets wrong — `bivacor-inc`, `fortuna-health`,
`patternbioscience`, `shockwave-medical`, `neko-health` — so this is
invariant 7 one layer earlier, exactly as the item argued.

**Two corrections to the fix as written above.** The embed URL is
`boards.greenhouse.io/embed/job_board/**js**?for=<slug>`; the form in step 1
omits the `/js` and would have matched none of the real `<script>` embeds
(it does appear in Apptronik's *iframe*, so both shapes are matched now).
And step 2 — writing the detected platform back to `ATS Platform` — was not
done: that field is a researched fact a human may have set, so invariant 3
applies. The detected slug is used for the run and the Scrape Note says what
was found and suggests the field change, leaving the edit to a person.

**The part the item did not anticipate,** and the reason most of
`tests/test_embedded_board.py` exists: a careers page can name boards that
are *not* the company's. Atomic VC's lists its portfolio companies' boards
(Exowatt, Hims & Hers) beside its own; Cellino's links to Osmo's. An early
version returned Exowatt for Atomic VC. Attaching another company's postings
is the same hazard as a guessed Workday tenant and equally undetectable
downstream, so each tier answers only if **unanimous** — one distinct board,
or it refuses and lets the caller fall back to guessing, which at least
fails visibly. Both aggregators are correctly refused and fall through to
the HTML scraper.

Teamtailor is excluded from detection: its pattern has to match a bare
careers-subdomain host, which against arbitrary page HTML matched almost
anything — one wrong answer and four false refusals. A Teamtailor board
embedded on a custom domain is therefore undetectable this way; accepted.

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

## 20. The Notion sync rewrote every row every run
**Critical** · `notion/client.py`, `notion/state.py`, `run.py`

**Done, 2026-09-09.**

Measured on a real run at 3,229 postings, with per-step wall clock added to
`main()` and request counters in `notion/client.py`:

| Step | Wall clock | Share |
| --- | --- | --- |
| 8 `sync_jobs_to_notion` | 2420.9 s | **80.3%** |
| 4 `run_scrapers` | 511.9 s | 17.0% |
| 7 `score_all` | 57.5 s | 1.9% |
| everything else | 24.2 s | 0.8% |
| **Total** | **3014.5 s (50 min)** | |

3,273 writes against 62 reads. Scoring was never the problem: it is cached, and
only 57 of 3,229 jobs needed an API call.

The cause was not a stray second pass. `sync_jobs_to_notion` built a full
twelve-property dict for every job and PATCHed it unconditionally — the
"New This Run" reset was field #12 of that same write, not a separate pass. It
*could not* have compared: `_query_all_job_postings()` only read back `page_id`,
`url`, `still_open`, `title` and the Company relation, so the values needed for a
diff were never fetched. Roughly 3,150 of those writes were byte-identical.

`sync_scrape_status()` had already solved this on the Target List and says so in
its docstring; the reasoning just never crossed over to Job Postings.

**Fix, as landed**
1. `_query_all_job_postings()` also reads back Score, Routing, Reasoning, Ambiguity
   Note, IC Role, Location, ATS, New This Run and Last Seen — out of the same
   paginated response, so no extra request.
2. `_changed_job_properties()` compares each owned property and returns only what
   moved. An unchanged row is not written at all.
3. `Last Seen` is written at creation and at close, never in between. The close
   stamp is the *previous* run's date (from `notion_state.json`), because the run
   that closes a posting is the run that did not see it.
4. `New This Run` is cleared only where Notion says the box is actually ticked —
   in a steady state, last run's creations and nothing else.
5. `output/notion_state.json` (`notion/state.py`) holds only the rubric fingerprint
   and the previous run's date. It is deliberately **not** a mirror of row state:
   that would be a second independent record of facts Notion already holds, which is
   the drift invariant 9 exists to prevent. The diff runs against Notion's own state.
6. The fingerprint is reporting only. A rubric change moves scores, and the value
   comparison notices that by itself; the fingerprint just lets the run explain a mass
   write before it happens. Gating on it *instead* of on the value would skip rows
   whose previous write failed and would never heal a hand-edited score.

Close-pass behaviour and the 25% safety valve are untouched — that pass was already
writing only rows that move, and the Scored List view filters on `Still Open`, so it
is load-bearing.

**The bug the delta pass exposed.** The first dry run after the change planned 803
writes, not the ~150 expected. 745 of them were the same field on different rows, and
the dry-run reason said why: `Routing: Needs Review -> Needs review`. Notion matches a
select name against its existing options case-insensitively, stores its own canonical
casing, and returns that on read. The pipeline emits `"Needs review"`; the option in
the database is named `"Needs Review"`. Every write was accepted and every read came
back different from what was asked for, so that field could never compare equal.

Fixed in the comparison (`_selects_equal`), deliberately not in the scorer:
`output_excel.py` filters its Needs Review tab on the scorer's literal, so
"correcting" the emitted string to match Notion would have silently emptied that tab.

Worth noting this was invisible before the delta pass — the old code rewrote every row
unconditionally, so a field that never compared equal cost nothing extra and left no
trace. It only became a bug worth finding once comparison started mattering.

`tests/test_delta_sync.py` (17 tests) covers the saving and, mostly, the failure mode
that replaces it: a comparison that can never be equal rewrites its row forever while
the sync still reports success. See invariants 12–15.

Deliberately **not** done here: concurrency. Cutting the wasted writes first was the
agreement; whether #13 is still worth doing is now a separate question against a much
smaller step 8.

## 21. Named ATS platforms with no scraper
**High** · `run.py` → `SCRAPERS`, `SLUG_URL_PATTERNS`, `scrapers/*.py`

**Done, 2026-09-21.**

An audit of all 470 `ATS Platform` values against `resolve_scraper()` found 25
companies whose ATS named a real platform nothing handled. The failure was
invisible in both directions: rows marked `API` got no scrape attempt at all and
showed as `No Scraper`, while rows marked `HTML` fell through to
`scrapers/html_generic.py` and were stamped `OK` with `HTML: 0 job(s)` — a
confident, wrong zero. ARCHIMED read `OK — 0` against a board of 23.

Seven scrapers added: Workable (12 companies), Personio (4), Breezy (3),
Rippling (2), Teamtailor (2), SmartRecruiters (1), Pinpoint (1). Live against
the real boards these return **106 postings** that were previously invisible.
Every endpoint was confirmed against the actual company boards rather than
assumed. Platforms with no usable public endpoint — iCIMS, Comeet, Jobvite,
Homerun, Paylocity, JazzHR, Polymer — were deliberately left alone rather than
guessed at.

Two did not fit the shape the other scrapers share, and both are recorded as
invariants rather than comments because the next scraper will meet them again:
Teamtailor's slug is a whole hostname (its customers run on their own domains),
and Personio answers a missing tenant with a 307 to its own marketing site
rather than a 404 — which the shared retry session then retried five times with
backoff against a site that never had the answer. See invariant 17.

**Left open**
- **BambooHR and Recruitee** (Proscia, Ibex) both look to have usable public
  endpoints, but neither could be tested: their saved Careers URL was the
  placeholder #22 is about, so no slug was knowable. Cheap to verify once the
  backfill saves real URLs for those two.
- **Two rows have contradictory data**, left for a human because ATS Platform is
  a researched fact (invariant 3): CoMind is tagged Personio but its Careers URL
  is an Ashby board holding 9 live roles, and Shockwave Medical is tagged
  `J&J Careers site` with a Workable URL. One field edit each.
- Three companies still fail honestly — a dead board (BrainsWay) and two whose
  real board URL is unknown (SimBioSys, Owlstone Medical). They surface in Needs
  Manual Check with a note naming the cause.

## 22. A wrong saved value is indistinguishable from a researched one
**High** · `notion/client.py` → `get_companies_missing_url()`,
`get_companies_missing_website()`; `ats_finder/careers_url.py`

**Partial, 2026-09-21.** Fixed for `Careers URL` and `Website`; the same
weakness in `ATS Platform`, `Sector / Focus` and `HQ` is untouched.

Invariant 8 makes a blank field mean "never researched", so every outcome writes
something non-blank. That is a sound test for a field whose wrong answers are
still *answers*, and no test at all for one holding something that was never an
answer. 20 rows carried the same pasted Notion document link in `Careers URL`.
Being non-blank, `get_companies_missing_url()` skipped them on every run while
the scrape retried and failed on every run — the one pass that could repair them
was the one pass that never looked.

It had already propagated. `derive_website()` reads the Careers URL, so 18 of
those rows had `Website` set to `https://app.notion.com` and their page icon
stamped from it, which invariant 16 reads as "already checked" — making the
wrong value permanent. Root cause there was a single omission: `NON_COMPANY_HOSTS`
listed `notion.so` and `notion.site` but not `notion.com`.

Both selectors now test the value rather than its presence. The bar is
structural impossibility, not suspicion, because acting on a rejection means
overwriting: the deliberate Google-search fallback and a merely stale URL both
still pass. See invariant 18 for the two constraints that must hold if the host
lists are ever widened.

The audit also surfaced a second bad-value shape the fix now catches: **88 rows**
hold a bare `"www.google.com"` — no scheme, no query, unusable by any scraper
(`requests` raises `MissingSchema`). 73 of those are excluded anyway by the
`"Not found"` ATS rule in #3; 15 are now repairable.

**Left open**
1. The same reasoning applies to `ATS Platform`, `Sector / Focus` and `HQ`, which
   still use the presence test. No demonstrated failure yet, so this was
   deliberately not widened on speculation — but a row whose ATS Platform is a
   whole sentence (Hocoma's is) is the same bug wearing a different field.
2. A cheap liveness check is **not** the fix and should not be bolted on here:
   invariant 1 means a fetch that fails to run is not evidence the URL is wrong,
   and a 404 is already a scrape failure the Needs Manual Check view surfaces.
   If it is ever wanted, it belongs beside #5/#19 as scrape-side work.

## 23. Duplicate rows stay "New This Run" forever
**High** · `scrapers/workable.py`, `run.py`, `notion/client.py`

**Status: done, 2026-09-29.** Found by querying the Job Postings database rather
than the code: 194 rows had `New This Run` ticked, but only 147 were actually
created that day. The other **47** had been ticked since a previous run — 39
Workable duplicates, 1 HTML duplicate (Insmed's "Analyst Coverage", from 9/15),
and 6 hand-added rows with no URL at all. 19 Workable URLs held **60 rows**
between them; most were created on 9/21, the first run with the Workable scraper.

**Why they stick.** Three separate things had to line up, which is why no single
pass caught it:

1. **The duplicates were created in one run.** `existing_by_url` is built once,
   before the create/update loop. A URL arriving a second time in the same run
   still reads as "not in Notion" and gets a row of its own.
2. **No later run can reach them.** `existing_by_url` is a dict: one row per
   URL. Every subsequent run updates whichever row that dict kept and clears
   *its* checkbox; the extras are never visited again.
3. **The close pass does not catch them either.** It only closes rows whose URL
   is *absent* from this run's scrape, and these URLs are still being scraped
   every run. So the extras keep both `Still Open` and `New This Run` set
   permanently.

The 6 URL-less rows fail for a simpler reason: the loop skips anything with no
URL at the top, and the close pass needs a URL to match on, so nothing in the
pipeline could ever touch their checkbox.

**Where the repeats came from.** Not pagination. Workable's widget endpoint
returns one entry *per location* for a multi-location posting — same
`shortcode`, same apply URL, only city/state/country differing. ARCHIMED's "HR
Internship (Lyon, France)" comes back six times (Lyon, Paris, Geneva, Brussels,
Madrid, Berlin). Confirmed against the live endpoint, and measured on the
2026-09-25 `jobs.json`: 43 of 4,242 postings were repeats of a URL already in
the file, 39 of them carrying the same title as the copy that was kept — a board
listing one job twice, not two jobs.

**Fix (all three landed)**
1. `scrapers/workable.py` collapses entries sharing an apply URL and *merges*
   their locations (`"Lyon, France / Paris, France / Geneva, France +3 more"`)
   rather than letting the first city win silently.
2. `run.py` → `dedupe_jobs_by_url()` runs between the scrape and scoring, so
   scoring, `jobs.json`, the workbook and the sync all see one job per URL. It
   is the net under the next platform that repeats a posting, and its log
   separates exact repeats (nothing lost) from one URL carrying several job
   titles (roles genuinely dropped — see #5).
3. `sync_jobs_to_notion()` gained two guards. A URL already written this run is
   skipped rather than created again, and — the blanket one — **every row Notion
   already held that is still flagged `New This Run` has the flag cleared at the
   end of the sync**, whether or not this run's scrape reached it. That last one
   is deliberately not three targeted fixes: the real failure is that the flag
   was only ever cleared as a side effect of visiting a row for some other
   reason, and clearing it on its own terms cannot be defeated by a fourth case
   nobody has thought of. Only rows whose box is actually set are written, so it
   costs nothing in a steady state (invariant 12).

Tests: `tests/test_duplicate_postings.py`, plus the Workable collapse cases in
`tests/test_new_scrapers.py` and the reset/one-row-per-URL cases in
`tests/test_delta_sync.py`.

**Left open**
1. **The 40-odd duplicate rows already in Notion are not repaired by this.**
   Choosing which of two identical rows to archive is a human judgement, and
   auto-closing a row whose URL *did* come back this run would contradict
   invariant 2. The sync now counts them and says so on every run, and the reset
   above at least takes them out of the New This Run view. Archive them by hand.
2. **A page that links several roles to one URL still loses roles** — Peritas AI
   (4) and Insmed (1) on the last run. That is punch list #5, and the answer is a
   per-role URL from the HTML scraper, not a dedupe.

## 24. Large MedTech companies no scraper reaches
**High** · `ats_finder/`, `scrapers/`

**Status: open, 2026-10-02.** The tier's dry runs left these in Needs Manual Check or
unresearched:

- **Masimo** — labelled Workday, but its postings sit on Danaher's shared board
  (`danaher.wd1…/DanaherJobs`). Needs the facet id that filters to Masimo; do not
  guess it (a mixed list attributed to one company is the failure `_verify_facets`
  exists to prevent).
- **Align Technology** — labelled Workday, but no Workday board exists. It uses
  iCIMS' Jibe front end (`aligntech.jibeapply.com`), which is not an `icims.com`
  portal and has a different API. The one company that would justify an iCIMS/Jibe
  scraper.
- **B. Braun** — RMK site whose result list is JavaScript-filled (item #25).
  **Steris, Straumann** — labelled SuccessFactors but not RMK sites; the real ATS is
  unidentified (Steris's `/us/en` and Straumann's `/global/en` pages look like a
  different product).
- **Siemens Healthineers, GE HealthCare, Enovis** — "in-house"; the page-source sniff
  found no embedded board. **Philips** (Phenom People), **Fisher & Paykel** (SnapHire)
  — no scraper for either platform.
- **Johnson & Johnson MedTech** — research reply hit the token cap; blank ATS, retries
  on the next real run. **Envista** — ambiguous between three companies; rename it
  (e.g. "Envista Holdings (dental)") and clear ATS Platform to re-research.
- **Convatec** — Workday scrape OK but zero senior roles of 113 postings; plausibly
  true, worth one look. **BD** — saved board is `EXTERNAL_CAREER_SITE_USA` only, so
  non-US senior roles are missed.

## 25. JS-rendered SuccessFactors sites need a browser
**Medium** · `scrapers/successfactors.py`

**Status: open, 2026-10-02.** `career{N}.successfactors.com/career?company=…` pages and
RMK sites like B. Braun fill their job list with JavaScript, so plain `requests` sees a
shell. The scraper raises "needs a browser" rather than returning zero jobs
(invariant 23), which is correct but leaves those companies uncovered. A browser path
needs a new dependency (Playwright), which CLAUDE.md says to ask about first. Check
first whether the page calls a JSON endpoint that can be read directly — that would
avoid the browser entirely.

## 26. SENIOR_TITLE_RE lets noise through
**Low** · `run.py`

**Status: open, 2026-10-02.** "Associate Director" and "Director" in a title like
"Executive Assistant, VP Commercial" both pass. Watch the first real run's Large
MedTech rows in New This Run; if the noise is high, options are excluding
`associate director` / `assistant to` / `executive assistant`, or dropping `head of`
/ `chief` / `general manager` (the brief expected to trim these). Any edit changes
which rows exist, not scores, so it does not touch the fingerprint — but a narrower
regex will make the close pass retire rows that were already synced.

## 27. resolve_scraper reads platform names out of negative text
**Medium** · `run.py`

**Status: done, 2026-11-16 (branch `fix/remaining-items`).** `resolve_scraper()` ignores a platform name preceded, in its clause, by a negation or example marker (not, no, like, such as, e.g., unlike, rather than ...). Siemens Healthineers' sentence now resolves to nothing. A negation in an earlier sentence does not poison a later claim. Tests: `test_negated_platform_names_resolve_to_nothing`, `test_real_platform_text_still_resolves`.

**Was: open, 2026-10-02.** Siemens Healthineers' ATS Platform is a long "in-house …
not a standard ATS like Greenhouse/Lever/Workday" sentence. `resolve_scraper()` matches
platform names anywhere in the text, so it matches three of them, warns, and uses
Greenhouse — a guessed-slug scrape of a company that has no Greenhouse board. It fails
safely today (the guess 404s) but a guessed slug can land on someone else's board. Fix:
skip names that follow "not"/"like"/"such as", or give in-house answers a fixed short
marker.

## 28. Scrapers can still sleep on a server's Retry-After
**High** · `http_client.py`, `scrapers/`

**Status: done, 2026-11-16 (branch `fix/remaining-items`).** Flipped at the source: every session in `http_client.py` now has `respect_retry_after_header=False`, so no scraper, Notion call or scorer call can sleep on a server's value; a bounded `backoff_factor` decides. This changes retry timing for Notion and the scorer too (a rate-limited call now backs off on our schedule instead of the server's, so it may retry sooner than asked). The Teamtailor and SuccessFactors cases from #46 are covered by the same flip. Tests: `test_no_session_honours_retry_after`, `test_retry_after_header_is_ignored_when_sleeping`. `CLAUDE.md` invariant 25 updated.

**Was: open, 2026-10-26.** The 2026-10-26 hang (changelog; invariant 25) was
fixed in `detect_embedded_board()` and `html_generic` (both now on
`http_client.third_party_session`; Sonivie's `Retry-After: 600` showed the second one
was needed). Every other scraper still calls the shared
`session` from `http_client.py`, which has `respect_retry_after_header=True`, so a
site answering 429/503 with a huge `Retry-After` can freeze a run the same way, with
its `timeout=` set correctly and doing nothing. Audit `greenhouse`, `lever` and
`ashby` first, then the rest. They talk to vendor APIs rather than arbitrary
sites, so the risk is lower than html_generic's was. Two gaps to check in each:
(1) is the call on a session with `respect_retry_after_header=False`; (2) does it pass
`timeout=`. All of them pass a timeout today (10–30 s) — gap 2 is a check, gap 1 is
the real work. Simplest fix is to flip the flag in `http_client.py` (a bounded
`backoff_factor` is enough for Notion and Anthropic too) or cap the sleep with a
`Retry.DEFAULT_BACKOFF_MAX`-style limit; either changes retry behaviour for Notion
and the scorer, so do it deliberately. Also confirm the `try` around each scraper call
in `run_scrapers` covers everything that can raise.

---

# 2026-11-16 audit (#29-#51)

Found by reading the code at commit `48d7891`; nothing was run. "Confirmed" means the
code path was traced and any stated precondition was checked in the docs or in
the private repo's run summary. "Plausible" means the mechanism is real but the trigger is
unverified. **None of the fixes below changes the fingerprint or `SCORER_VERSION`, and none
triggers a re-score.** Every fix wants an offline test beside it.

## 29. A failed embedded board falls through to HTML and closes the company's rows
**High** · `run.py:881-897` · confirmed

**Status: done, 2026-11-16 (batch A, branch `fix/audit-batch-a`).** Done as specified: a board named by detection whose scraper raises is recorded `SCRAPE_FAILED` and `continue`s, so the company never enters `scraped_ok`. Test: `test_failed_embedded_board_is_failed_not_ok`. In the HTML branch of `run_scrapers()`, when
`detect_embedded_board()` names a board and that board's scraper raises, the code
falls through to `html_generic` on the same page. An embed or redirect page has no
job-shaped links, so it returns `[]`, the company is stamped OK and added to
`scraped_ok`, and the close pass closes every one of its rows. Neko Health (127 jobs,
a redirect case) would lose all of them to one Ashby outage that outlasts the retries;
the next run reopens them, not flagged new and with a wrong `Last Seen`.

**Fix:** when detection named a board and its scraper raised, `record(row,
SCRAPE_FAILED, ...)` and `continue`. Do not fall through. Invariant 2. Test: embedded
scraper raises -> company not in `scraped_ok`, status Failed.

## 30. SmartRecruiters postings change URL at the 100-detail cap
**High** · `scrapers/smartrecruiters.py:36,100-116` · confirmed

**Status: done, 2026-11-16 (batch B, branch `fix/audit-batch-b`).** `MAX_DETAIL_FETCHES` raised 100 -> 2000; a board over it raises `ScrapeShapeError`. A failed detail call, or a detail reply with no `postingUrl`/`applyUrl`, now raises for the whole company instead of emitting the bare-id URL; a posting with no id is skipped. Expect the one-time churn of ~26 Brainlab rows on the first run after this lands. Tests: `test_smartrecruiters_126_postings_all_use_the_detail_url`, `..._detail_failure_fails_the_company`, `..._detail_without_a_url_...`, `..._board_over_the_cap_raises`. Rippling untouched. The URL is `postingUrl` (id plus a title suffix) when
the detail call ran and the bare-id fallback when it did not. Only the first
`MAX_DETAIL_FETCHES` (100) postings get a detail call. Brainlab is the only
SmartRecruiters company and has 126 postings, so 26 carry the fallback URL, and which
26 depends on list order. A posting crossing index 100, or one transient detail
failure, changes its URL: the old row closes and a new one is created as New This Run
(invariant 20).

**Fix:** raise the cap well above the board size, and when a detail call fails raise
for the whole company (it lands in Failed and invariant 2 protects its rows) instead
of emitting a second identity. Expect a one-time churn of ~26 Brainlab rows on the
first run after the fix. The existing "keeps the posting when its detail call fails"
style of test needs rewriting for SmartRecruiters; Rippling's URL comes from the list
and is not affected.

## 31. score_cache.json is rewritten non-atomically after every batch
**High** · `scoring/score_jobs.py:94-97,384`, `scoring/track_new_postings.py:32-35` · confirmed

**Status: done, 2026-11-16 (batch A, branch `fix/audit-batch-a`).** `_save_cache()` and `_save_seen()` write a `.tmp` beside the file, then `os.replace()`. Tests: `test_failed_cache_write_leaves_the_old_file_intact`, `test_cache_and_seen_round_trip`. `_save_cache()` rewrites the whole 11.8 MB file with a
plain `open(..., "w")` after every batch of five. A Ctrl-C or crash mid-write leaves
truncated JSON and `_load_cache()` then raises on every later run. A full re-score is
~900 rewrites. The only backups are the ones `restamp_score_cache.py` makes.

**Fix:** write to a temp file in the same directory, then `os.replace()`. Same for
`_save_seen()`. Standard library only.

## 32. A guessed slug is accepted as the company's board
**High** · `ats_finder/find_ats.py:432-436`, `run.py:794,806-821` · plausible

**Status: partly done, 2026-11-16 (batch C, branch `fix/audit-batch-c`).** Steps (1) and (2) landed; step (3) (checking a guessed board's own name against the company) is **still open**. (1) `find_company_info()` lets `try_known_patterns()` fill a blank or unresolved ATS but never replace a researched one. (2) In `run_scrapers()` the guessed-slug retry after a failed saved slug now happens only when the saved slug answered 404 (`_answered_404()`); a timeout, 429 or 5xx is recorded Failed. **Not changed:** the branch where no slug can be read from the saved URL still guesses one, since that has no saved slug to answer 404 and needs (3). Tests: `test_guess_does_not_overwrite_a_researched_platform`, `test_guess_may_still_fill_an_unresolved_answer`, `test_guessed_slug_is_tried_only_after_a_404`, `test_timeout_or_5xx_on_the_saved_slug_does_not_guess`. Three entry points, one failure (another company's
postings attached, invisibly — invariant 19's failure by a different route):
- `find_company_info()` lets `try_known_patterns()` overwrite the researched ATS
  whenever `slugify(name)` answers 200 on Greenhouse, Lever or Ashby. That proves a
  board with that slug exists, not that it is this company's.
- `run_scrapers()` guesses a slug when none can be read from the saved URL.
- When a saved slug fails for ANY reason, including a timeout, the guessed slug is
  tried and a success enters `scraped_ok`.

**Fix:** (1) never let a guess override a researched platform; (2) fall back to a
guessed slug only when the saved slug answered 404, not on a timeout or 5xx;
(3) ideally check the board's own name against the company before accepting a guess.
(3) may need design — do (1) and (2) first. Related: #27.

## 33. A failed score overwrites Routing, Reasoning and IC Role
**Medium** · `scoring/score_jobs.py:401-409`, `notion/client.py:548-574` · confirmed

**Status: done, 2026-11-16 (batch A, branch `fix/audit-batch-a`).** `_changed_job_properties()` skips every scorer-owned field (Score, Routing, Reasoning, Ambiguity Note, IC Role) when the job's score is `None`; `_failure_result()` is unchanged. Test uses the real failure shape: `test_real_failure_shape_writes_nothing`. Invariant 15's wording in `CLAUDE.md` corrected. Invariant 15 says a failed job comes back with `score`
and `routing` of `None`. `_failure_result()` actually returns routing `"Needs
review"`, a "Scoring failed: ..." reasoning, an ambiguity note and `ic_role_flag`
False. The diff skips only Score, so an existing good row keeps its score but is
moved to Needs Review and loses its Reasoning until the next successful run. Most
likely during a full re-score with rate-limited batches.
`tests/test_delta_sync.py:365-372` passes `routing=None`, a shape the scorer never
produces.

**Fix:** in `_changed_job_properties()`, when the job's `score` is `None`, skip every
scorer-owned field (Routing, Reasoning, Ambiguity Note, IC Role). Do NOT change
`_failure_result()`'s literals — `output_excel.py` reads them. Rewrite the test with
the real failure shape, and correct invariant 15's wording in `CLAUDE.md`.

## 34. Row creation is transparently retried and can duplicate
**Medium** · `http_client.py:21-27`, `notion/client.py:1327` · plausible

**Status: done, 2026-11-16 (batch B, branch `fix/audit-batch-b`).** New `http_client.create_session` (retries only 429 and connect errors, `read=0`) used by `_create_job_posting()` only; `respect_retry_after_header` left as the patient session has it, for #28 to decide. Tests: `test_create_session_retries_only_429_and_connect_errors`, `test_create_job_posting_posts_through_create_session`. The shared session retries POST on 502/504 and on read
timeouts. If Notion committed the page before the gateway error, urllib3 re-sends the
create and a second row exists for the same URL — which invariant 20 says no later
run can reach.

**Fix:** send `_create_job_posting()` through a session that retries only 429 and
connect errors (keep `respect_retry_after_header` as #28 decides). A create that
fails is already reconciled by URL on the next run.

## 35. scraped_ok and the Company relation are keyed by name
**Medium** · `run.py:735`, `notion/client.py:712,875` · confirmed precondition

**Status: done, 2026-11-16 (batch B, branch `fix/audit-batch-b`).** `prepare_jobs()` stamps `company_page_id` on each job; `scraped_ok` is a set of page ids; the sync takes the relation from the job's own id (name lookup only as a fallback for a job with no id); the close pass tests `row["company_page_id"] in scraped_ok`. `test_close_pass.py`, `test_delta_sync.py`, `test_large_medtech_filter.py` and `test_url_based_plumbing.py` updated to page ids. Tests: `test_failed_twin_is_not_closed_by_its_successful_namesake`, `test_job_relation_uses_the_jobs_own_page_id`, `test_scraped_ok_holds_page_ids`, `test_prepare_jobs_stamps_the_page_id`. `docs/project-status.md` records two company names
that each appear twice (Neuvotion, NeuroOne Medical Technologies). `scraped_ok` is a
set of names and `company_page_by_name` keeps one page per name. If one row fails and
its twin scrapes OK with zero jobs, the name is in `scraped_ok` and the failed row's
postings close (invariant 2).

**Fix:** stamp `company_page_id` on each job in `prepare_jobs()`, make `scraped_ok` a
set of page ids, and have the sync use the job's page id for the relation and the
close pass compare `row["company_page_id"]`. Tests in `test_close_pass.py` and
`test_delta_sync.py` pass names today and need updating.

## 36. Five scrapers read a missing key as an empty board
**Medium** · `scrapers/greenhouse.py:12`, `ashby.py:22`, `workable.py:85`, `pinpoint.py:65`, `teamtailor.py:59` · plausible

**Status: done, 2026-11-16 (batch A, branch `fix/audit-batch-a`).** New `scrapers.common.require_list()` raises `ScrapeShapeError` on a missing key or non-list; used by Greenhouse, Ashby, Workable, Pinpoint and Teamtailor. An empty list under the key is still a real empty board. Tests parametrised over all five. The `html_generic` and per-company-guard follow-ups were not touched. `.get("jobs", [])` (and `"data"`, `"items"`) returns
`[]` for a 200 reply that lacks the key (invariant 23). The 25% valve cannot see a
single-platform false zero: all 1,227 Greenhouse rows are under it. Related and NOT
part of this fix: `html_generic` returns `[]` for any page with no job-shaped links,
so a JS redesign silently closes a company and stamps it OK.

**Fix:** raise `scrapers.common.ScrapeShapeError` when the key is absent; an empty
list under the key stays a real empty board. Optional follow-up worth its own
decision: a per-company guard that holds a "had open rows, now zero" result for one
run and says so in the Scrape Note.

## 37. Oracle and SuccessFactors always scrape senior terms only
**Medium** · `scrapers/oracle_rc.py:101`, `scrapers/successfactors.py:102`, `run.py:756` · confirmed

**Status: done, 2026-11-16 (batch C, branch `fix/audit-batch-c`).** `run.py` passes `SENIOR_SEARCH_TERMS` to Oracle and SuccessFactors for a tiered company and the new `ALL_POSTINGS_TERMS` (`[""]`, an empty keyword) otherwise. Both scrapers also now raise `ScrapeShapeError` when a board still has postings after `MAX_PAGES` pages, because a full-board read can exceed the cap and a silently truncated read would make every later posting look gone to the close pass. **Unverified live:** that an empty `keyword=` returns the whole board on Oracle; the SuccessFactors empty `q=` is likewise untested against a real site. Check with `--dry-run` on the first untiered company on either platform. Tests: `test_untiered_company_reads_the_whole_board`, `test_tiered_company_keeps_the_senior_terms`, `test_empty_keyword_is_sent_as_an_empty_query`, `test_a_board_longer_than_max_pages_raises_instead_of_truncating`. Both default `search_terms` to `SENIOR_SEARCH_TERMS`
and `run.py` never passes any, so an untiered company on either platform silently
loses every non-senior posting — against invariant 22's "every other company keeps
every posting". Harmless only while every such company is tiered.

**Fix:** pass terms by tier as the Workday branch does (`run.py:731`), and page the
full board (empty keyword) when the company is untiered.

## 38. An interrupted sync loses New This Run on rows it created
**Medium** · `notion/client.py:811`, `run.py:989-1002` · confirmed

**Status: done, 2026-11-16 (batch B, branch `fix/audit-batch-b`).** `sync_jobs_to_notion()` rewrites `output/carry_new_this_run.json` (atomically, carried ids included) after each real create and deletes it when the sync runs to the end; `run.py` no longer removes it itself and reads the path from `notion.client.CARRY_NEW_PATH`. `tests/conftest.py` gained an autouse fixture pointing that path at a temp dir, so no test touches the real file. Tests: `test_interrupted_sync_leaves_the_created_ids_behind`, `test_completed_sync_deletes_the_carry_file`, `test_dry_run_writes_no_carry_file`, `test_carried_ids_from_an_earlier_run_are_kept_...`. A run killed mid-sync has already created some rows.
The next run reads them as existing and clears the flag, so they are never shown as
new. `output/carry_new_this_run.json` exists for exactly this but nothing writes it.

**Fix:** have the sync append each created page id to that file as it goes (real
runs only — thread `dry_run`) and delete it when the sync completes. `run.py`
already reads and removes it. Tests must point the path at a temp dir.

## 39. Scorer output is not validated before use
**Medium** · `scoring/score_jobs.py:391,422-432` · plausible

**Status: done, 2026-11-16 (batch C, branch `fix/audit-batch-c`).** `_parse_json_array()` raises `_BatchParseError` unless the indices are exactly `0..n-1`; each element is reduced to the six result keys; `score` is coerced (int, integral float or digit string) and anything outside 0-5, or a bool, makes that one posting a failure result (score `None`, retried next run) rather than failing the batch. `score_all()` copies onto a job only the result keys plus `rubric_fp`/`rubric_hash`/`previous`. No prompt, model or fingerprint change. First tests for the module: `test_duplicate_or_missing_indices_fail_the_batch`, `test_extra_keys_are_dropped_from_a_result`, `test_scores_are_coerced_and_range_checked`, `test_invalid_score_makes_that_posting_a_failure_...`, `test_a_cached_result_cannot_overwrite_the_jobs_identity`. Three gaps:
- `_parse_json_array()` checks the count but not that the indices are exactly
  `0..n-1`, so a duplicated index assigns a score to the wrong job.
- `job.update(result)` copies every key the model returned, so an echoed `url` or
  `title` would overwrite the job's identity.
- A string score such as `"0"` routes as Scored and fails the Notion write every run.

**Fix:** raise `_BatchParseError` unless indices are exactly `0..n-1`; copy only
`score`, `reasoning`, `ambiguous`, `ambiguity_note`, `ic_role_flag`, `routing` (plus
the provenance keys); coerce `score` to int and treat anything outside 0-5 as a
failure. None of this changes the prompt, so no fingerprint change. The module has no
tests at all today — add them here.

## 41. rubric.md is read with the Windows default encoding
**Medium** · `scoring/score_jobs.py:47,51` · confirmed

**Status: done, 2026-11-16 (batch A, branch `fix/audit-batch-a`).** `encoding="utf-8"` on both opens in `scoring/score_jobs.py` (the rubric and, for the same reason, the keywords file). Test: `test_rubric_is_decoded_as_utf8`. `open(_RUBRIC_PATH)` has no `encoding=`. This machine
is cp1252 with UTF-8 mode off, and the rubric has em-dashes on lines 25 and 29, so
the model receives `â€”` there. A curly closing quote pasted into the rubric would
raise `UnicodeDecodeError` at import and stop the run before it starts.

**Fix:** `encoding="utf-8"` on both opens. It changes `rubric_hash` going forward
(provenance only, never compared) and invalidates nothing.

## 42. Scrape Note comparison can never be equal for connection errors
**Low** · `notion/client.py:393-395,425` · plausible

**Status: done, 2026-11-16 (batch C, branch `fix/audit-batch-c`).** `_normalise_scrape_note()` strips `object at 0x...` and truncates to 2,000; it is applied to the new note, the stored note being compared, and the write. Notes already stored with an address will differ once, then settle. Tests: `test_connection_error_note_is_not_rewritten_every_run`, `test_over_long_note_is_not_rewritten_every_run`, `test_object_address_is_stripped_and_long_notes_truncated`. Invariant 13's trap in `sync_scrape_status()`.
Connection-error text contains `object at 0x...`, which differs every run, so a
DNS-dead company's note is rewritten every run. A note over 2,000 characters is
written truncated but compared untruncated.

**Fix:** strip `object at 0x[0-9A-Fa-f]+` from the note and truncate to 2,000 before
both the comparison and the write.

## 43. Personio host fallback changes URL identity
**Low** · `scrapers/personio.py:83-120` · plausible

**Status: done, 2026-11-16 (batch B, branch `fix/audit-batch-b`).** `.de` is tried only after a definite no-such-board answer on `.com`: the 307 redirect, **or a 404** (a small widening of the item's wording, so the existing `test_personio_falls_back_to_the_de_host` stays valid). Timeouts, 5xx and bad XML propagate. Tests: `test_personio_timeout_on_com_does_not_fall_to_de`, `..._5xx_...`, `test_personio_redirect_on_com_still_falls_to_de`. Any failure on `.com`, including a timeout, falls to
`.de`. For a tenant that answers on both, every posting URL changes host, so the
company's rows all close and re-create.

**Fix:** fall to `.de` only when `.com` gave the redirect ("no such board") answer;
re-raise anything else.

## 44. Scheme-less URLs from research loop or blank-stamp
**Low** · `ats_finder/find_ats.py:346-348`, `ats_finder/find_website.py:153-154,177` · plausible

**Status: done, 2026-11-16 (batch C, branch `fix/audit-batch-c`).** New `ats_finder.careers_url.ensure_scheme()` (a dot, no whitespace, no scheme: nothing else is touched) used in `_clean_url()` and `find_website_homepage()`. Tests: `test_ensure_scheme_is_narrow`, `test_research_reply_with_a_scheme_less_url_is_saved_with_one`, `test_scheme_less_homepage_answer_yields_a_host`. `_clean_url()` returns the raw text when it finds no
`http(s)://` token, so `example.com/careers` is saved, fails
`is_usable_careers_url()`, and is re-researched (paid) every run. In
`find_website_homepage()` a scheme-less answer yields no host, and the company gets
the permanent blank icon (invariant 16) despite a real answer.

**Fix:** when the value has a dot and no scheme, prepend `https://` in both places.

## 45. HTML scraper mojibake and unjoined relative links
**Low** · `scrapers/html_generic.py:226,268-271`, `ats_finder/find_embedded_board.py:165` · confirmed / plausible

**Status: done, 2026-11-16 (batch C, branch `fix/audit-batch-c`).** New `http_client.response_text()` sets the detected encoding when the response declares no charset; used by `html_generic.fetch_jobs()` and `find_embedded_board.py`. Every href goes through `urljoin`; fragment-only, `javascript:`, `mailto:` and `tel:` hrefs are no link and fall back to the careers URL as a missing href always did. Expect one-time title and URL churn on HTML companies. Tests: `test_undeclared_charset_uses_the_detected_one`, `test_declared_charset_is_trusted`, `test_hrefs_are_joined_and_non_links_dropped`, `test_generic_scrape_uses_joined_links_and_falls_back_for_non_links`. `resp.text` decodes as ISO-8859-1 when the server
sends no charset; the private repo's run summary shows it in a real title ("PA Â· Full-time",
confirmed). Only hrefs starting with `/` are joined to the page URL, so `#`,
`javascript:` and bare relative links are stored as-is and can collide across
companies in `dedupe_jobs_by_url()` (plausible).

**Fix:** set `resp.encoding = resp.apparent_encoding` when the response declares no
charset, in both files. `urljoin` every href; treat fragment-only, `javascript:` and
`mailto:` hrefs as no link. Titles will change once (one write each); URLs for
relative links will change identity once. Overlaps #5.

## 46. sniff_ats_from_page honours Retry-After on third-party sites
**Low** · `ats_finder/find_ats.py:37-43,139` · confirmed

**Status: done, 2026-11-16 (batch C, branch `fix/audit-batch-c`).** `sniff_ats_from_page()` fetches through `third_party_session`. `try_known_patterns()` keeps `_guess_session` on purpose. The Teamtailor and SuccessFactors cases were **not** touched: they are folded into #28 as the item says. Test: `test_sniff_fetches_through_the_third_party_session`. It fetches arbitrary company careers pages on
`_guess_session`, which has `respect_retry_after_header=True` (invariant 25).
Teamtailor and SuccessFactors also hit company-owned domains on the patient session.

**Fix:** use `http_client.third_party_session` for the sniff. Fold the Teamtailor and
SuccessFactors cases into #28.

## 47. Hardcoded Job Postings fallback id looks stale
**Low** · `notion/client.py:27` · plausible

**Status: done, 2026-11-16 (batch A, branch `fix/audit-batch-a`).** Fallback removed: `os.environ["JOB_POSTINGS_DATABASE_ID"]`. Mirror section of `CLAUDE.md` updated (one scrub fewer). Test: `test_job_postings_id_has_no_fallback`. The fallback differs from the Job Postings id recorded
in `docs/project-status.md` after the September workspace move. With the env var
missing, the sync would target the old database.

**Fix:** `os.environ["JOB_POSTINGS_DATABASE_ID"]`, no fallback, as the public mirror
already does. Update the mirror section of `CLAUDE.md` (one scrub fewer).

## 48. A blank-named Companies row costs a paid search every run
**Low** · `notion/client.py:164-166`, `ats_finder/find_ats.py:49-50` · plausible

**Status: done, 2026-11-16 (batch A, branch `fix/audit-batch-a`).** `_drop_unnamed()` applied to the four `get_companies_missing_*` selectors and a skip at the top of `run_scrapers()`, each printing how many were skipped. Tests: `test_selectors_skip_unnamed_rows`, `test_run_scrapers_skips_unnamed_rows`. `test_website_backfill.py`'s fixture rows gained a `company` name. An empty row has company `None` and a blank ATS, so
`fill_missing_ats()` researches "None" (billed), then `slugify(None)` raises, which
is logged as "will retry next run".

**Fix:** skip rows with no company name in the `get_companies_missing_*` selectors
and in `run_scrapers()`, and print how many were skipped.

## 49. Senior search terms lack "vp" and "svp"
**Low** · `scrapers/common.py:27`, `run.py:125-128` · plausible

**Status: done, 2026-11-16 (batch C, branch `fix/audit-batch-c`).** `"vp"` and `"svp"` added to `SENIOR_SEARCH_TERMS`. Test: `test_senior_terms_include_vp_and_svp`. Newly found postings are new rows and will be scored. `SENIOR_TITLE_RE` accepts `vp` and `svp` but the search
terms do not include them, so abbreviated titles at tiered companies are found only
when another term happens to match.

**Fix:** add both terms. More requests per tiered company; newly found postings are
new rows and will be scored.

## 50. Unanimity is not ownership in board detection
**Low** · `ats_finder/find_embedded_board.py:226-235` · plausible · **decision**

**Status: left as is, 2026-11-16 (the owner's decision).** Tier 3 accepts a page that links exactly one board. A
VC firm's page linking one portfolio company's board passes, and that board is
scraped as the firm's own. The Scrape Note says so, but under status OK, which no
view surfaces. Options: require the slug to resemble the company name for tier 3
only, or leave it. No fix proposed.

## 51. --dry-run overwrites jobs.json and the workbook
**Low** · `run.py:1028-1044` · confirmed

**Status: left as is, 2026-11-16 (the owner's decision).** A dry run rewrites `output/jobs.json`,
`skipped_companies.json` and `job_postings.xlsx` with rehearsal data. It breaks no
stated guarantee, but the local artifacts then describe a run that never synced.

**Fix:** under `dry_run`, skip the three writes or write them beside the real ones
with a `.dry-run` suffix.

## Suggested batching

One branch and one PR per batch. This order is not the priority order.

| Batch | Items | Why together |
|---|---|---|
| 1 | **#4, then #1** | Fingerprint first, so the description change actually forces a re-score. #1 alone leaves every existing job frozen on its title-only score. |
| 2 | **#7, then #2 and #3** | Retries first. #2 and #3 are the same problem in two places: telling a transient failure apart from a real answer. Run #3's recovery pass at the end. |
| 3 | **#6 (done), #9, #10, #12, #16** | All small, independent, cleanup. One sitting. |
| 4 | **#14, #13, #17** | Cache persistence, run time, observability. Do these *before* the first scheduled run. |
| 5 | **#5, #8, #11, #15** | #5 is the biggest coverage win left and the most open-ended. |

Batch 1 will trigger a full re-score. Back up `output/score_cache.json` first and
budget for it.

### 2026-11-16 audit batches (#29-#51)

None of these triggers a re-score. After each batch: `python -m pytest tests/`, then
`python run.py --dry-run` and read the planned writes, and only then a real run.

| Batch | Items | Why together |
|---|---|---|
| A | **#29, #31, #33, #36, #41, #47, #48** | Stop data loss. All small, independent, a few lines plus a test each. |
| B | **#30, #35, #34, #38, #43** | Row identity. #35 touches the tests the others build on, so do it first. Expect ~26 Brainlab rows to churn once after #30. |
| C | **#32, #37, #39, #42, #44, #45, #46, #49** | The rest. #32 step (3) may need design; land steps (1) and (2) regardless. |
| — | **#50, #51** | Left as is by the owner's decision, 2026-11-16. Not in any batch; do not implement. |

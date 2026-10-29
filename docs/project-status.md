# Project status

*Originally written Aug 17–18, 2026. Ported into the repo Aug 22, 2026. Last updated
Oct 2, 2026.*

Background and history for anyone (including Claude) picking this up cold. For the
active work list see `docs/punch-list.md`. For working rules see `CLAUDE.md`.

## What this project is

An automated pipeline that tracks job postings across ~1,500 target companies (medtech,
digital health, healthcare-focused PE/VC), scores them against a custom fit rubric,
and syncs results into a live Notion dashboard. The repo owner holds the architecture decisions
and business logic (the rubric); Claude wrote the implementation.

## Where everything lives

- **Code** — the pipeline itself, in a private repo; this is its sanitized public
  mirror. Runs locally via `python run.py`. Not yet on a schedule.
- **Company data** — Notion "Companies" database (renamed from "Target List" on
  2026-09-10; same id, same data source, display name only), `<redacted>`
  (data source `<redacted>`). 1,502 rows as of 2026-10-29.
- **Job data** — Notion "Job Postings" database, `<redacted>`
  (data source `<redacted>`). Related to Companies via a
  "Company" relation. 11,057 rows (8,368 open) as of 2026-10-29.
- **Dashboard** — Notion "Active Opportunities" page, `<redacted>`.
  Active Roles, To Do, Job Boards (ExecuNet, Reccy, Kidneyverse/Signals), and link
  cards to both databases.
- **Rubric** — `scoring/rubric.md` in this repo is the authoritative copy and the only
  one the code reads. The Claude Project doc "Scoring Rubric" is a stamped read-only
  mirror of it, synced Sep 9, 2026; Head of Product is no longer missing from it. Edit
  the repo copy, then re-stamp the Project copy from it. Punch list #6 is closed.
- **Profile doc** — "Profile details" in the Claude Project. Used for framing and
  pitch angle when writing outreach. Not read by the code.

## Cost controls already built in

- Haiku rather than Sonnet for scoring, batched and prompt-cached (though see punch
  list #8 — the caching may not actually be firing).
- Scores are cached and fingerprinted, so a run pays only for jobs it has never scored
  under the current rubric. On the Sep 9 run, 57 of 3,229 postings needed an API call
  and scoring was 1.9% of the wall clock.
- Every "found nothing" outcome writes a persistent marker rather than leaving a
  blank, so a company is never re-searched once genuinely checked. Applies to ATS,
  Sector, HQ, and Careers URL independently.
- A search that *fails with an error* is treated differently from a search that *runs
  and finds nothing*. Errors leave the field untouched for retry. (Punch list #3 is a
  hole in this: a truncated model reply is neither, and currently lands in the
  permanent bucket.)
- Companies already marked "Not found" at the ATS level are excluded from the separate
  Careers URL search.
- The Job Postings sync is delta-only: every owned property is compared against what
  Notion already holds, and a row with no differences is not written at all. A
  steady-state run writes ~100 rows rather than ~3,200.
- `--dry-run` rehearses a run with no Notion writes and no Anthropic spend, and prints
  *why* each write would happen. It is not an offline mode — it still reads Notion and
  still scrapes public ATS endpoints.

## Bugs already found and fixed

Listed for pattern-matching if similar symptoms reappear.

- **Exact-match ATS scraper matching** → whole-word regex, so `"Greenhouse (acquired
  by Getinge)"` still resolves.
- **Careers URL field type mismatch** → code wrote it as text, the Notion column is
  URL type. Fixed the write and the silently broken read.
- **Search failures writing false "not found" markers** → only a genuine empty result
  writes a marker now; an error leaves the field alone.
- **URLs saved with commentary baked in** (`https://x.com/careers (jobs listed via
  https://y.com)`) → added `_clean_url()`, which extracts the real URL and prefers a
  known-ATS domain over a generic homepage.
- **API scrapers always guessing the slug from the company name** → now extracts and
  prefers the confirmed slug from the saved Careers URL, guessing only as fallback.
  This is why Adaptive Innovations, CMR Surgical, and Capstan Medical kept 404ing.
- **"New This Run" Notion view was sort-only** → real per-run checkbox property,
  reset every run, filtered properly.
- **Scored List view showed empty** → stray leftover filter restricting it to one
  company; view rebuilt.
- **A failed scrape closed that company's live postings** (punch list #2) → only
  companies whose scraper actually returned may have rows closed, and a run that would
  close more than 25% of open rows aborts the close pass outright.
- **One curly quote killed a run mid-pipeline** (punch list #18) → Python 3.14 on
  Windows defaults stdout to cp1252; `run.py` now reconfigures both streams to UTF-8
  at import.
- **`--dry-run` wrote to `seen_jobs.json`** → a rehearsal was quietly consuming the
  "first seen" signal the next real run depended on.
- **The Job Postings sync rewrote all ~3,200 rows every run** (punch list #20) → 80%
  of a 50-minute run, and it *could not* have compared, because the read-back query
  never fetched the values a diff would need.
- **A select comparison that could never be equal** → found by the first delta dry
  run, which planned 803 writes instead of the expected ~150. Notion matches select
  names case-insensitively and returns its own casing, so the pipeline's
  `"Needs review"` read back as `"Needs Review"` and 745 rows rewrote that field every
  run. Fixed in the comparison, deliberately not in the scorer — `output_excel.py`
  filters its Needs Review tab on the scorer's literal. Invariant 13 in `CLAUDE.md`
  records the other three traps of this shape.

- **Duplicate rows stayed "New This Run" forever** (punch list #23) → Workable's
  widget endpoint lists a multi-location posting once per city, and
  `existing_by_url` is built once before the create loop, so each repeat in that
  run got its own Notion row. A dict holds one row per URL, so no later run could
  ever visit the extras: 19 Workable URLs held 60 rows, and 47 rows had been
  sitting in the New This Run view for up to two weeks. Fixed at three levels —
  collapsed at the source, deduped run-wide before scoring, and a blanket
  end-of-sync reset that clears the flag on every row this run did not create.

## Open issues not in the punch list

- **Scrape coverage is the biggest gap.** On the Sep 9 run, 87 companies produced
  3,132 postings and 203 produced nothing scrapeable: 169 with no scraper (mostly
  companies whose ATS research came back "Not found"), 32 whose scrape failed, and 2
  with no Careers URL. Those surface in the Needs Manual Check view on Companies,
  sorted by `Failing Since` so the oldest breakage is first. Punch list #5 and #19
  cover the code side; the headless-browser question for JavaScript-rendered job
  lists is still open.

  Sep 21 (#21) took 25 companies out of that bucket. It also showed the "no scraper"
  count was the *optimistic* reading of the gap: 15 of those 25 were not in Needs
  Manual Check at all, but stamped `OK` with `HTML: 0 job(s)` — the generic HTML
  scraper reporting a confident zero against a live board. A false `OK` is worse than
  a `No Scraper`, because nothing surfaces it. How many of the remaining `OK — 0`
  companies are the same thing is not yet known and is worth measuring before more
  scrapers are written.

  Partly answered the same day by #19's sweep: of the 198 HTML-marked companies
  with a Careers URL, 20 were reaching a real ATS board the generic scraper was
  reading as ordinary HTML. Those are now scraped properly and contribute 379
  postings. The remaining ~178 are genuine HTML pages or JS-rendered listings;
  the latter still need a headless browser (#5).

- **A third of the database is outside the careers-URL backfill.** 162 of 470 rows
  carry a `"Not found"` ATS value, and `get_companies_missing_url()` excludes all of
  them by design — re-searching a URL for a company already answered conclusively is
  waste. 36 of those read `"Not found (auto-search failed)"` exactly, which punch list
  #3 argues may be truncated replies rather than real answers. Until #3's recovery step
  runs, those rows cannot be repaired by any pass. This is the reason the #22 fix helps
  fewer rows than the raw count of bad URLs suggests.
- **Arbital Health** — ATS field still says "Greenhouse and/or Lever (ambiguous)" and
  defaults to Greenhouse. On the Sep 9 run it scraped without error and returned zero
  postings, so the ambiguity is unresolved but not currently failing.
- **CMR Surgical** — still failing on Greenhouse. Its saved board URL 404s and its
  real careers pages contain no ATS reference in the raw HTML, which points at a
  JS-rendered listing rather than the embed gap punch list #19 describes. Read the
  investigation note on that item before treating it as a #19 example.
- **Genuinely ambiguous company names** — Signals Group, BioHope Scientific, Montagu,
  Ortivity. No confirmed ATS or careers page, all still No Scraper. Not necessarily
  wrong, just unresolved.
- **No outcome feedback loop.** Date Applied and Application Notes exist on Job
  Postings, but nothing uses that data to recalibrate the rubric. This is the planned
  "closed loop" phase, and it is explicitly human-reviewed recalibration rather than
  automated retraining, given realistic data volume.

## Roadmap

1. Work the punch list, batch by batch.
2. Schedule via GitHub Actions. **Do punch list #14 first** or every scheduled run
   re-scores from scratch.
3. Build the outcome-tracking → rubric-refinement loop.
4. Email digest of new well-scored postings.

## Changelog

- **Oct 26, 2026** — Fixed a ~3-hour hang in the scrape step. A run froze after
  Carolina Instruments; Ctrl+C showed it asleep in urllib3
  `Retry.sleep_for_retry` -> `time.sleep(retry_after)`, called from
  `detect_embedded_board()` for the next HTML-marked company. That site answered
  429/503 with a very large `Retry-After`, the shared session in `http_client.py`
  honoured it, and the request `timeout` does not cover a retry sleep. Fix:
  `find_embedded_board.py` now has its own session (total=2, backoff_factor=1,
  `respect_retry_after_header=False`); both `detect_embedded_board` call sites in
  `run_scrapers()` are wrapped in `try/except Exception` and a failure is recorded
  as `Failed` with a Scrape Note, so one bad site no longer aborts the loop; a
  `[detect] <company>` line prints before each call; and every call in
  `notion/client.py` now passes `timeout=30` (there was none). New invariant 25.
  The fix first landed in `find_embedded_board.py` only; the next real run then stalled
  9+ minutes on Sonivie (503 with `Retry-After: 600`), this time in
  `html_generic.fetch_jobs`, which fetches the same page through the shared session.
  The no-`Retry-After` session therefore moved to `http_client.third_party_session`
  and both modules use it. The patient session and the API scrapers on it are
  unchanged — punch list #28.

- **Oct 2, 2026** — Added a **Large MedTech** tier: 37 companies over $1B revenue
  that are scraped in full but only kept, scored and synced when the title is senior
  (VP / SVP / Director / Head of / General Manager / Chief). Six commits:
  (1) `Company Tier` read into `row["tier"]`; `prepare_jobs()` filters in every
  `run_scrapers` branch before `all_jobs.extend`; the scorer's per-job message gains
  a tier line while the system prompt and cache key stay put. (2) **Rubric change:**
  removed the deal-breaker "Public company role requiring investor relations or
  public-market experience". By invariant 10 this invalidates the score cache; the
  next real run re-scores every job (6,232 cache entries) unless
  `restamp_score_cache.py --apply` is run first (it would keep 5,924 and leave the 306
  that cite the rule). Not yet run. (3) 30 companies added to Companies (7 of the 37
  already existed and were tiered in place); `sniff_ats_from_page()` reads a careers
  page's source for a hidden Workday / Oracle / iCIMS / SuccessFactors board before
  an in-house answer is accepted. Five Workday Careers URLs (BD, Dexcom, Convatec,
  Integer, Elekta) were replaced with their real boards. (4) `URL_BASED_PLATFORMS`,
  `PLATFORM_ALIASES`, `scrapers/common.py`; the existing Workday scraper gained a
  search-term mode and a shape check. (5) Oracle Recruiting Cloud scraper (Cooper,
  ICU Medical). (6) SuccessFactors RMK scraper (Coloplast, Bausch + Lomb, Sonova,
  Boston Scientific, Terumo, Teleflex). Dry-run results: Workday 15 of 18 companies OK
  (Convatec 0 kept), Oracle 2 of 2, SuccessFactors 6 of 9. **Not built:** iCIMS (no
  company on it), Avature and Taleo (Boston Scientific, Terumo and Teleflex had been
  mislabelled from their `/go/` URL shape; they are SuccessFactors). The full pipeline
  has not been run since.
- **Sep 29, 2026** — Fixed duplicate Job Postings rows that stayed flagged
  `New This Run` indefinitely (#23). Found in the data, not the code: 194 rows
  were ticked and only 147 had been created that day. Root cause was Workable
  returning a multi-location posting once per city, multiplied by an
  `existing_by_url` map built before the create loop — so repeats within one run
  became rows nothing could reach afterwards. Workable now collapses per-city
  repeats and merges their locations, `run.py` dedupes every scrape by URL before
  scoring, and the sync clears `New This Run` on every pre-existing row at the end
  of the run rather than only on the rows it happened to visit. The ~40 duplicate
  rows already in Notion are counted and reported each run but archived by hand:
  picking which copy to keep is a human call, and auto-closing a row whose URL is
  still being scraped would contradict invariant 2.
- **Sep 21, 2026 (later)** — Closed both remaining Critical punch list items.
  #3: a research reply that ran out of tokens no longer becomes a permanent
  "Not found (auto-search failed)" — `stop_reason` is checked before the text is
  parsed, the cap went 500 → 1500, a reply carrying none of the expected fields
  raises, and `_extract()` anchors to line starts and takes the last match rather
  than the first (it was returning the model's own narration on replies that
  narrated before answering). The recovery pass for the 36 existing markers is
  written (`reset_failed_ats_research.py`) but deliberately not run: it costs ~36
  research calls. #19: confirmed the gap was real by sweeping all 198
  HTML-marked companies with a Careers URL — **20** reach an ATS board nothing
  was reading, worth **379 postings**, and 10 have a slug the name-guess gets
  wrong. `ats_finder/find_embedded_board.py` detects them via redirect, embed or
  link, and refuses rather than guessing when a page names several boards, which
  is what stops an aggregator's portfolio listings being attached to it (Atomic
  VC's page yields Exowatt to a naive reading). New invariant 19.
- **Sep 21, 2026** — Audited all 470 `ATS Platform` values against `resolve_scraper()`
  and added the seven scrapers the audit called for (#21): Workable, Personio, Breezy,
  Rippling, Teamtailor, SmartRecruiters, Pinpoint — 25 companies, **106 postings** that
  the pipeline could not previously see. 15 of those companies had been reporting
  `OK — HTML: 0 job(s)`, a confident wrong zero, rather than showing up in Needs Manual
  Check at all. The audit then exposed a data-integrity gap (#22): 20 rows held a pasted
  Notion document link in `Careers URL`, which — being non-blank — made them permanently
  invisible to the careers-URL backfill while the scrape failed on them every run, and
  which had already propagated into `Website` for 18 of them. Both selectors now test
  the value, not just its presence. Cleared the 20 bad URLs in Notion via
  `reset_placeholder_careers_urls.py`. New invariants 17 and 18.
- **Sep 10, 2026** — Added a `Website` property to Companies plus a pipeline step,
  `fill_missing_website()`, that derives it (free extraction from the Careers URL first,
  an Anthropic homepage search only if that fails) and stamps the page icon with a real
  favicon logo or a plain white square either way — see `ats_finder/find_website.py`
  and invariant 16 in `CLAUDE.md`. Roughly 200 of ~440 companies were backfilled by hand
  first (real domain + favicon), to spot-check icon quality before trusting the
  heuristic at scale; the rest — including the "checked, nothing found" white-square
  stamp — are left for `fill_missing_website()` to finish on the next real run.
- **Sep 10, 2026** — Notion content moved to a new dedicated workspace (Companies,
  Job Postings, and the Active Opportunities dashboard duplicated across, relation to
  Companies confirmed intact, originals then moved to Trash). New database ids; `.env`
  updated with a new token. Separately, `scrapers/html_generic.py` and its tests picked
  up a same-day fix for false-positive job postings — see git log for that commit's
  message and detail.
- **Sep 9, 2026** — Delta-only Job Postings sync (#20): the sync now reads back every
  property it owns and writes only the rows that moved, cutting a steady-state run
  from ~3,273 writes to ~100. Found and fixed the select-casing comparison bug in the
  process. Added `notion/state.py`, per-row `Scrape Status` / `Scrape Note` /
  `Failing Since` stamping with a Needs Manual Check view, a Workday scraper, and
  tests for the delta sync, the scrape status pass, Workday, and the close pass.
  Closed #6: the repo rubric is the single source of truth and the Claude Project copy
  is now a stamped read-only mirror.
- **Aug 25, 2026** — Documented the public mirror and its porting checklist in
  `CLAUDE.md`. Gitignored all of `output/`, not just its `.json` files.
- **Aug 24, 2026** — #2: the close pass now runs only against companies that scraped
  successfully, with a 25% valve and a 20-row floor. Stopped `--dry-run` writing to
  `seen_jobs.json`. Added punch list #19.
- **Aug 22–23, 2026** — #4 then #1: fingerprinted the score cache and fed posting text
  to the scorer, which forced a full re-score, paid for once. #7: one shared HTTP
  session with retries, plus a `--dry-run` flag. #18: forced UTF-8 stdout/stderr. #15
  resolved as intentional. Excluded sales representative titles from scoring.
- **Aug 22, 2026** — Full code review against commit `243b2f2`; produced
  `docs/punch-list.md` (17 items, since grown to 20). Added `CLAUDE.md`. Ported this
  doc into the repo.
- **Aug 20, 2026** — Pushed to GitHub as `Job-Search-Agent` (initial commit).
- **Aug 17–18, 2026** — Added Head of Product to the rubric. Added Date Applied,
  Application Notes, Application Summary to Job Postings. Fixed the bug list above.

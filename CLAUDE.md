# CLAUDE.md

## Read this first: this is a public mirror

This repository is a **sanitized, public copy** of a private pipeline, kept as a
portfolio piece. It is *supposed* to differ from its private counterpart, and the
differences are load-bearing:

| File | How it differs | Why |
|---|---|---|
| `notion/client.py` | the repo owner is not named in four comments | no personal names in a public repo (the Job Postings id has no fallback in either copy as of 2026-11-16) |
| `ats_finder/find_ats.py` | extra "can be adapted to any industry" line in the research prompt | this copy is a template others can reuse |
| `.env.example` | every id is `xxxx` | nothing real belongs in a public repo |
| `scoring/rubric.md` | no owner name, worked-example criteria | it is a sample, not a live job search |
| `scoring/excluded_title_keywords.json` | generic entries | same |
| `docs/project-status.md` | all Notion ids redacted | same |
| `CLAUDE.md` | this section; no owner name, no porting checklist | the checklist lives upstream, and describing it here would be describing this repo from the outside |

**Never reconcile this repo against the private one.** If you are shown both and the
differences read like drift, they are not drift. "Restoring consistency" would publish
a private database id to a public repo.

Nothing in `output/` is ever committed here, and no real `.env` exists.

Guidance for Claude Code working in this repository.

## What this is

A pipeline that tracks job postings across 1,506 target companies (as of 2026-11-13) (medtech, digital
health, healthcare-focused PE/VC), scores each posting against a personal fit
rubric, and syncs the results into a Notion dashboard plus an Excel workbook.

The repo owner holds the architecture decisions and the business logic (the rubric).
Claude wrote and maintains the implementation. The rubric in `scoring/rubric.md` is a
worked example — swap it for your own criteria and the rest of the pipeline is
unchanged.

## Running it

```bash
pip install -r requirements.txt
python run.py --dry-run        # rehearsal: no writes, no API spend
python run.py                  # the real thing
python -m pytest tests/        # the offline checks
python output_excel.py         # rebuild the workbook from existing output/*.json
python archive_old_postings.py  # dry-run: closed postings first seen >6 months ago (--apply to archive)
python close_nonsenior_tiered_postings.py  # one-off, dry-run: close open non-senior rows at senior-filter tiers (--apply)
python clear_new_flag_nonsenior_biotech.py # one-off, dry-run: untick New This Run on non-senior Large Biotech rows (--apply)
```

Requires a `.env` in the repo root with `NOTION_API_KEY`, `NOTION_DATABASE_ID`,
`JOB_POSTINGS_DATABASE_ID`, and `ANTHROPIC_API_KEY`. See `.env.example`.
**Never read, print, echo, or commit `.env`.**

A real run writes to Notion and spends money on the Anthropic API. Treat
`python run.py` as a production action, and reach for `--dry-run` first.

**What `--dry-run` does and doesn't guarantee.** It skips every Notion write and
every Anthropic call, and prints what it would have done instead. It still
performs read-only Notion queries and still scrapes public ATS endpoints, so it
is not an offline mode. Any new code path that writes or spends must be threaded
with the flag — a dry run that mutates anything is worse than no dry run, because
you will trust it.

## Pipeline order (`run.py` → `main()`)

1. `fill_missing_ats()` — blank ATS Platform means "new, unresearched company". Full
   profile lookup via Claude + web search.
2. `fill_missing_profile_fields()` — has an ATS but missing Sector or HQ.
3. `fill_missing_careers_urls()` — any company missing a Careers URL. Fallback chain:
   dedicated careers page → homepage → pre-filled Google search.
4. `fill_missing_website()` — any company with neither a `Website` value nor a page
   icon yet. Extracts the domain from the (now-backfilled) Careers URL for free where
   possible, an Anthropic homepage lookup otherwise; stamps the page icon with a real
   favicon logo or a blank-square marker either way. See
   `ats_finder/find_website.py` and invariant 16.
5. `run_scrapers()` — eleven platforms via their public APIs (Greenhouse, Lever,
   Ashby, Workday, Workable, Personio, Breezy, Rippling, Teamtailor, SmartRecruiters,
   Pinpoint); everything else marked HTML gets `scrapers/html_generic.py`. Returns
   `(jobs, scraped_ok, statuses)`.
   Workday takes its own branch: its board needs four parameters rather than one slug,
   and it deliberately has no guess-from-the-company-name fallback.
   Two of the newer ones are not shaped like the rest. Teamtailor's "slug" is a whole
   hostname, because its customers run on their own domains rather than a shared vendor
   path. Personio answers a missing tenant with a 307 to the vendor's marketing site
   rather than a 404, so `scrapers/personio.py` does not follow redirects — see
   invariant 19.
6. `dedupe_jobs_by_url()` — collapses postings that share a URL, keeping the first
   of each, so every later step sees one job per URL. See invariant 22.
7. `sync_scrape_status(statuses)` — stamps every Target List row with how its scrape
   went. Anything not `OK` is what the Needs Manual Check view shows.
8. `mark_new_postings()` — flags first-seen postings against `output/seen_jobs.json`.
9. `score_all()` — batched, cached, fingerprinted, Haiku, rubric as system prompt.
10. `sync_jobs_to_notion(jobs, scraped_ok)` — dedup by URL, create/update, close what's
    genuinely gone, then clear `New This Run` on everything it did not create.
    **Delta-only**: every owned property is compared against what Notion already holds
    and only differences are written. A steady-state run writes ~100 rows, not ~3,200.
    Two things ride along with the same pass: each posting inherits its company's page
    icon (invariant 17), and `_backfill_applied_dates()` stamps `Date Applied` on rows
    hand-marked `Application Status` = Applied (invariant 18).
11. `build_workbook()` — five-tab Excel export.

## Invariants

These encode bugs that were already found and fixed. Breaking one reintroduces a
real failure, so preserve them unless the task explicitly says otherwise.

1. **An error is not an answer.** A search that *failed to run* (network, API outage,
   billing) must never write a "Not found" marker. It leaves the field untouched so a
   future run retries. Only a search that *ran and genuinely found nothing* writes a
   marker. This distinction is the entire retry-safety design.
2. **Never close a row for a company that didn't scrape successfully.** Absence from
   this run's results is not proof a posting is gone; it is equally consistent with a
   timeout. Only companies in `scraped_ok` (a set of Companies **page ids**, since two rows can share a name) may have rows closed, a row whose Company
   relation doesn't resolve is never closed, and a run that would close more than 25%
   of open rows (once there are at least 20) aborts the whole close pass.
3. **Never overwrite a non-empty Notion field during backfill.** Callers pass `None`
   for fields they don't want touched; `update_company_info()` skips them. The one
   exception is `sync_scrape_status()`, which owns `Scrape Status` / `Scrape Note` /
   `Failing Since` outright: those are a stamp the pipeline computes, not researched
   facts a human may have corrected. Nothing else may copy that.
4. **Routing is computed, never trusted.** The model returns a `routing` field, but
   `_enforce_routing()` overwrites it from score + ambiguity flag. Keep it that way.
5. **`Careers URL` is a Notion URL-type property**, not rich text. Empty must be
   `None`, not `""` — Notion rejects the empty string.
6. **ATS matching is whole-word regex, not exact.** Values carry human context like
   `"Greenhouse (acquired by Getinge)"`. See `resolve_scraper()`.
7. **Prefer the confirmed slug from the saved Careers URL** over slugifying the
   company name. Guessing was why verified companies 404'd every run.
8. **Every "nothing found" outcome writes a non-blank value.** A blank field is the
   signal for "never researched", so leaving it blank means paying to re-search that
   company forever.
9. **Notion is authoritative for "New This Run"**, not `output/seen_jobs.json`. Two
   independent records of the same fact drifted once already.
10. **A rubric edit applies going forward; everything else that changes a score must
    change the fingerprint.** `_prompt_fingerprint()` hashes the model id, the keyword
    list, and `SCORER_VERSION`. The rubric text is deliberately *not* hashed (the owner's
    decision, 2026-10-05): editing `scoring/rubric.md` leaves cached scores alone and
    only newly scored postings see the new rubric. Each cache entry records
    `rubric_hash` for provenance. To force a full re-score after a rubric edit, bump
    `SCORER_VERSION`. Change the model, keyword list or prompt shape without bumping
    one of those and the cache serves stale answers forever, silently.
11. **`Failing Since` dates the outage, not the run.** It is set when a company
    crosses from OK into any failure status and left alone for as long as it keeps
    failing, so the Needs Manual Check view can be sorted oldest-first. Re-stamping
    it every run would make every long-broken company look like it broke today,
    which is the one thing that view is there to tell you.
12. **The Job Postings sync writes only what changed.** Every owned property is
    compared against what `_query_all_job_postings()` read back, and a row with no
    differences is not written at all. Rewriting all of them unconditionally is what
    made the sync 80% of the run at 3,200 rows. Any new field the sync writes has to
    be read back and compared too, or it silently reintroduces a full-table write.
    The page icon is not a property and so isn't part of the property diff, but it
    obeys the same rule by a different mechanism — read back per row as `has_icon`,
    written only where absent. See invariant 17.
13. **A comparison that can never be equal is worse than no comparison.** It rewrites
    its row every run while the sync still reports success, so the saving evaporates
    with nothing to show it. Four real traps, all covered by tests in
    `tests/test_delta_sync.py`. Normalise both sides identically:
    - **Selects are matched case-insensitively by Notion, which then returns its own
      casing.** The pipeline emits `"Needs review"`; the option is named
      `"Needs Review"`. This one actually escaped — it was 745 of the 803 writes left
      after the first delta pass. Compare selects with `casefold()`. Do *not* fix it
      by changing the scorer's literal: `output_excel.py` filters its Needs Review tab
      on that same string and would silently empty.
    - Notion returns `None` for an empty rich_text where the scraper returns `""`.
    - A Score written as `7` reads back as `7.0`.
    - `_build_job_properties()` truncates text at 2000 chars, so a longer value can
      never read back equal to what was handed in.

    The reason these are findable at all is that `--dry-run` prints *why* each write
    is planned, not just that it is. "Routing: Needs Review -> Needs review" named the
    bug outright. Keep that reason string if you touch the dry-run logging.
14. **`Last Seen` is written at creation and at close, never in between.** An open
    posting's live status is what `Still Open` answers. The close pass stamps it with
    the *previous* run's date from `notion_state.json`, because the run that closes a
    posting is by definition the run that did not see it. No view sorts or filters on
    it — check that again before making it load-bearing.
15. **`None` from the scorer means "no answer this run", not "clear the field".** A
    job whose scoring failed comes back with `score` of `None` (and, from
    `_failure_result()`, a placeholder routing "Needs review", a "Scoring failed"
    reasoning and `ic_role_flag` False); `_changed_job_properties()` skips every
    scorer-owned field — Score, Routing, Reasoning, Ambiguity Note, IC Role — when
    the score is `None`, rather than blanking or downgrading a good row. Same
    convention as `update_company_info()`.
16. **A company's page icon, not just `Website`, marks it as checked.** `Website` is a
    URL-type property, so it can't hold a text "Not found" marker the way `ATS Platform`
    does — writing empty means "never checked" (invariant 8) for every field except this
    one. `fill_missing_website()` stamps the page icon either way — a real favicon on
    success, the blank-square marker (`ats_finder.find_website.BLANK_ICON`) on a genuine
    "nothing found" — so `get_companies_missing_website()` filters on "no icon yet", not
    "no Website yet". A row a human iconed by hand for an unrelated reason will look
    already-checked and won't be retried; accepted trade-off, not a bug.
17. **A job posting's icon is copied from its company, never derived.** Job Postings
    has no icon logic of its own, so a row stays blank forever unless the sync copies
    one across from the related Target List row. Only an `external` company icon
    carries a URL that can be copied — the emoji `BLANK_ICON` a genuine "no website
    found" leaves behind (invariant 16) and a Notion-hosted `file` icon both read back
    as `None` and are skipped rather than guessed at. This is deliberately the one
    write that happens with an empty property diff: a row whose `has_icon` is false is
    written even when nothing else about it moved, because it will never reach the
    create branch again. The cost is bounded — each row is iconed once — but the first
    run after this landed, and any run after a batch of companies gains favicons, will
    report an `updated` count far above the usual steady state. That is the backfill,
    not a regression of invariant 12.
18. **`Date Applied` is only ever stamped onto a blank.** `Application Status` is
    maintained by hand; stamping the date the instant it changes is a Notion database
    automation, which needs a paid plan, so `_backfill_applied_dates()` is the free
    substitute. It writes today's date only where `Application Status` is `Applied`
    **and** `Date Applied` is empty. A date already present — typed by hand or stamped
    by an earlier run — is never overwritten, which is invariant 3's rule applied to a
    field the pipeline half-owns, and no other status value is touched. It runs over
    every row `_query_all_job_postings()` returned rather than over this run's scrape,
    so it still reaches a posting marked Applied after it closed and stopped being
    scraped; the create/update loop never revisits those.

19. **A "board not found" that answers with a redirect must not be followed.** Most
    boards 404 an unknown slug. Personio instead answers 307 with
    `Location: https://personio.com` — the vendor's *marketing site*, which answers
    429. Following that hands a 429 to the shared retry session in `http_client.py`,
    which retries it five times with growing backoff: one wrong slug becomes a slow
    retry storm against a site that was never going to have the answer, and the
    Scrape Note a human reads ends up saying "too many 429 error responses" with no
    mention of the board. `scrapers/personio.py` passes `allow_redirects=False` and
    reads a 3xx as "no such board". Any future scraper for a platform that redirects
    rather than 404s needs the same treatment — check before assuming a 404.
20. **Blank is not the only "never researched" state — a structurally impossible
    value is one too.** Invariant 8 makes blank mean unresearched, which works for a
    field whose wrong answers are still answers and not at all for one holding
    something that was never an answer. A batch of rows carried a pasted Notion link
    in `Careers URL`: non-blank, so `get_companies_missing_url()` skipped them every
    run while the scrape retried and failed every run, and `derive_website()`
    propagated it into `Website` — where the page icon (invariant 16) then marked it
    permanent. Both selectors now test the value, not just its presence:
    `ats_finder/careers_url.py`'s `is_usable_careers_url()` and
    `find_website.is_company_domain()`. Two things keep that safe, and both must
    hold if the lists are ever widened:
    - **The bar is structural impossibility, not suspicion.** Acting on a rejection
      means overwriting, so anything a human deliberately chose has to pass. The
      pre-filled Google *search* is the case that matters — `fill_missing_careers_urls()`
      writes it as its documented last resort, and rejecting it re-opens the
      re-research-forever loop invariant 8 closed. Only the `docs.`/`drive.`
      subdomains are listed; `google.com` itself is not.
    - **Liveness is never judged here.** A stale or 404ing URL still passes. That is
      a scrape failure, which `sync_scrape_status()` already surfaces, and calling it
      invalid would collide with invariant 1.
    The paired write matters too: `fill_missing_website()` passes `website or ""`, so
    a genuine "nothing found" *clears* a bad stored value. Passing `None` there would
    leave it and the selector would reselect the row every run, forever.
21. **A careers page naming several ATS boards must never be resolved to one of
    them.** `ats_finder/find_embedded_board.py` reads a company's real board off its
    own careers page — a redirect, a `<script>`/`<iframe>` embed, inline JS, or a
    link — which is what lets 20 HTML-marked companies be scraped through their
    actual ATS instead of being read as flat HTML. The hazard is that a page can
    name boards that are not the company's: Atomic VC's careers page lists its
    portfolio companies' boards (Exowatt, Hims & Hers) beside its own, and Cellino's
    links to Osmo's. An early version returned Exowatt for Atomic VC. Attaching
    another company's postings is the same failure as a guessed Workday
    tenant (invariant 2's neighbourhood) and equally undetectable downstream — the
    rows look perfectly normal.

    So each tier answers only if **unanimous**: exactly one distinct (platform, slug).
    Not "the most common", which is precisely the reasoning that picks Exowatt off a
    page listing eight portfolio boards. On a tie it returns None and the caller
    falls back to guessing a slug from the name, which at least fails visibly.

    Two consequences worth keeping: Teamtailor is excluded from detection entirely,
    because its slug pattern has to match a bare careers-subdomain host and so matches
    almost any careers URL on an arbitrary page (measured: one wrong answer, four
    false refusals); and a detected platform is used for the run but never written
    back to `ATS Platform`, which is a researched fact a human may have set —
    invariant 3. The Scrape Note says what was found and suggests the edit instead.

22. **A posting's URL is its identity: one row per URL, and one write per URL per
    run.** The Job Postings sync builds `existing_by_url` as a dict before its
    create/update loop, so a URL arriving twice in one run reads as new both times
    and gets two rows — and a dict holds one row per URL, so no later run can ever
    visit the second one again. It keeps `Still Open` and `New This Run` set
    forever, and the close pass cannot retire it either, because its URL *is* still
    being scraped. 19 Workable URLs held 60 rows between them this way.

    The repeats were real, not a scraping bug in the usual sense: Workable's widget
    endpoint lists a multi-location posting once per city, same apply URL each time.
    That is collapsed at source in `scrapers/workable.py` (locations merged, not
    first-city-wins), `run.py`'s `dedupe_jobs_by_url()` catches whatever the next
    platform does, and the sync skips a URL it has already written this run. Three
    layers because the damage is done in the sync but the cause is not visible
    there. Rows that are already duplicated cannot be repaired from code — picking
    which copy to archive is a human judgement, and auto-closing one whose URL came
    back this run would contradict invariant 2 — so the sync counts them and says so
    on every run instead.

23. **`New This Run` is cleared by a pass of its own, over every row Notion already
    held.** It means "created by THIS run", and it used to be cleared only as a side
    effect of visiting a row for some other reason — the update branch, or the close
    pass. Anything neither pass reached stayed ticked indefinitely: duplicate rows
    (invariant 22), the six rows added by hand with no URL at all, and rows whose
    company failed to scrape. 47 of the 194 flagged rows on 2026-09-29 were stuck
    like that, the oldest since 2026-09-15. The reset runs at the end of
    `sync_jobs_to_notion()` and writes only rows whose box is actually set, so it
    costs nothing in a steady state (invariant 12). Fixing the three known cases
    individually was the tempting version and the wrong one: the failure is the
    side-effect clearing itself, and a fourth case would have gone the same way.

24. **The senior filter runs before anything downstream sees a job.** For a
    `Company Tier` in `SENIOR_FILTER_TIERS` (Large MedTech, Large Biotech, Large
    Misc), `prepare_jobs()` in `run.py` drops every title
    that does not match `SENIOR_TITLE_RE`, inside `run_scrapers()`, before
    `all_jobs.extend()`. `mark_new_postings()`, `score_all()` and the sync all read
    that list, so a filtered role that got past it would be scored (money) and
    written to Notion (clutter) before anything could remove it. A new branch that
    extends `all_jobs` must go through `prepare_jobs()` too — it also copies
    `company_description` and the tier onto each job. The filter is per-company-tier
    only; every other company keeps every posting.

25. **A scraper must raise on a response it cannot parse; it returns `[]` only for
    a board it recognised as empty.** "Page loaded, my selectors found nothing" is
    indistinguishable from an empty board unless the scraper insists on positive
    evidence, and a false zero is the failure Workday-on-HTML produced for every
    Workday company (and the close pass reads it as "all postings gone"). The new
    scrapers raise `scrapers.common.ScrapeShapeError`: Oracle raises on a reply with
    no `items[0].requisitionList` (which is also what caught a missing `expand`
    parameter during development); SuccessFactors believes a page only with rows
    plus a total, or the server-rendered "Search results for" heading with no rows;
    Workday raises on an answer with neither `jobPostings` nor `total`.

26. **A platform label written from a URL shape is a guess.** Boston Scientific,
    Terumo and Teleflex were stamped Avature/Taleo from `/go/<category>/<id>/` URLs
    that are actually SuccessFactors RMK sites. Check a label against what the
    scraper can read before building a scraper for it — two scrapers were nearly
    written for platforms nobody was on.

27. **Every outbound HTTP call needs a request timeout *and* a Retry adapter with
    `respect_retry_after_header=False`; a per-company failure never aborts
    `run_scrapers`.** `timeout=` bounds a connect or a read, not the sleep urllib3
    does between retries: `Retry.sleep_for_retry` obeys a server's `Retry-After`
    header, and a site answering 429/503 with a huge value froze a run for ~3 hours
    inside `detect_embedded_board()` with every timeout correctly set. So
    `http_client.third_party_session` (total=2, backoff_factor=1, header ignored)
    is what `find_embedded_board.py` and `scrapers/html_generic.py` use to fetch
    arbitrary company sites, and `notion/client.py` passes `NOTION_TIMEOUT` (30 s)
    on every call. **The patient `session` in `http_client.py` no longer honours
    `Retry-After` either** (punch list #28): every session there has
    `respect_retry_after_header=False`, so only our own bounded `backoff_factor`
    decides how long a retry waits. Any
    call that touches a third-party site outside a `try` in `run_scrapers` is a way
    for one company to kill the run; `detect_embedded_board` is now wrapped, and
    the `[detect] <company>` line printed before it is what names the culprit
    next time.

28. **A full read of Job Postings must be sliced; Notion silently stops a single
    query at 10,000 rows.** `has_more` comes back false and the rest is simply not
    delivered, with no error. The database held 11,057 rows (8,368 open) while
    `_query_all_job_postings()` returned exactly 10,000, so the create-vs-update
    lookup by URL, the close pass and the delta comparison were all blind to the
    overflow: a posting past the cutoff reads as new and gets a second row
    (invariant 22), and a gone one is never closed. The read now walks calendar
    months of `created_time`, halves any window that comes back at
    `JOB_QUERY_CAP`, and raises if a window cannot be halved. Do not "simplify" it
    back to one query. `created_time` is used because every row has it; it is **not**
    a measure of age, since the September 2026 workspace move re-created every row
    in two months. Age is `First Seen`.

    **Archiving** (`archive_old_postings.py`, `select_archivable_postings()`) moves
    a row to Notion's trash (restorable 30 days) only if it is closed, has a URL, has
    no Application Status or Date Applied, and has a First Seen older than
    `ARCHIVE_AFTER_DAYS`. A blank First Seen means unknown age, which stays. It is
    dry-run by default and is deliberately **not** wired into `run.py`.

## Traps

- **`output/` is gitignored in full (except `.gitkeep`) but holds real state.** `score_cache.json` (~500 KB)
  represents money already spent. `seen_jobs.json` is run history. `notion_state.json`
  holds the previous run's date, which is what a closing posting's `Last Seen` gets
  stamped with. Never delete or regenerate these casually. Tests must stub
  `notion.state` rather than let it touch the real file — a test run that advances
  `last_run` to today makes the next real run stamp closing postings with the date of
  a test.
- **A rubric edit does not re-score anything** (invariant 10): only new postings use
  it. A change to the scoring prompt shape, the model, the keyword list, or
  `SCORER_VERSION` invalidates the entire cache and triggers a full re-score of
  thousands of jobs, which costs real money. Say so before it happens.
- **`scoring/rubric.md` is the only rubric the code reads.** If you keep a second copy
  anywhere outside the repo — a doc, a wiki, a chat project — that copy is a read-only
  mirror of this file, not a second original: edit the repo copy and re-stamp the other
  from it. An edit to the copy changes no behavior.
- **`Application Status` is read by the pipeline and written by nobody.** It is the
  one Job Postings property a human owns outright, and `_backfill_applied_dates()`
  matches its value against the exact string `"Applied"`. Notion matches select names
  case-insensitively but returns its own casing (invariant 13), so renaming the option
  or re-spelling it — `applied`, `Applied ✅` — makes the backfill quietly stop finding
  anything. It will report `0` stamped, which is indistinguishable from "nothing to
  stamp". If the count is 0 on a run where a row was definitely marked Applied, check
  the option's spelling in Notion before looking at the code.

- **`scoring/excluded_title_keywords.json` matches on substring**, deliberately. A new
  entry catches every title containing the word, management roles included.
- Repo is on Windows, Python 3.14, where stdout defaults to cp1252. `run.py`
  reconfigures stdout and stderr to UTF-8 with `errors="replace"` at import. Don't
  remove it — one curly quote in a job title would otherwise kill a run mid-pipeline.

## Notion schema

**Target List** (`NOTION_DATABASE_ID`): Company (title), Sector / Focus (text), HQ
(text), Source (text), ATS Platform (text), Scrape Method (text), Careers URL (**url**),
Jobs (relation → Job Postings), Scrape Status (select: OK / Failed / No Scraper /
No Careers URL), Scrape Note (text), Failing Since (date), Website (**url** — see
invariant 16 and `fill_missing_website()`).

The Target List page **icon** is also pipeline-owned: a Google favicon logo when
`fill_missing_website()` derives a real domain, a plain white square when it genuinely
can't. Don't treat a blank Website with no icon and a blank Website with an icon as the
same state — see invariant 16.

Views on Target List: Default view, Needs Manual Check (Scrape Status is not OK,
sorted by Failing Since ascending — the oldest breakage first).

**Job Postings** (`JOB_POSTINGS_DATABASE_ID`): Job Title (title), Company (relation →
Target List), Score (number), Routing (select: Scored / Needs Review / Non-fit),
Reasoning (text), Ambiguity Note (text), IC Role (checkbox), Location (text), URL (url),
ATS (text), First Seen (date), Last Seen (date), Still Open (checkbox), New This Run
(checkbox), Application Status (select — hand-maintained, see invariant 18), Date
Applied (date), Application Notes (text), Application Summary (formula).

The Job Postings page **icon** is pipeline-written but not pipeline-derived: it is a
copy of the related Target List row's icon, and only when that icon is an `external`
one. See invariant 17.

Views on Job Postings (five):

| View | Filter | Sort |
| --- | --- | --- |
| Scored List | `Routing` = Scored AND `Still Open` = true | First Seen desc, Score desc |
| New This Run | `New This Run` = true | Score desc |
| Applications | `Date Applied` is not empty | Date Applied desc |
| Non-fits | `Routing` = Non-fit | First Seen desc |
| Total list | none | none |

There is deliberately no Needs Review view — "Needs Review" is a `Routing` select
option, not a view. It shows as a column, and those jobs get seen in New This Run, so
a whole tab for them was noise.

Two consequences worth keeping in mind before changing the sync:

- **Scored List filters on `Still Open`**, so the close pass is load-bearing for the
  main working view. Do not weaken it; the 25% safety valve stays as it is.
- **`Last Seen` is a displayed column in all five views and is sorted or filtered on
  by none of them.** That is what makes invariant 14 safe. If a view ever starts
  sorting on it, invariant 14 has to be revisited rather than worked around.

## Current work

`docs/punch-list.md` holds 22 prioritized improvements with fix-level detail, a status
line per item, and a suggested batching order at the bottom. Work one batch per branch.
`docs/project-status.md` has the longer history, including bugs already fixed and open
issues.

When asked to work on a numbered item, read that item in full before editing, and
check its cross-references — several items depend on another landing first. Update the
item's status line and the summary table when it lands.

## Large MedTech tier and URL-based scrapers

MedTech companies over $1B revenue (37 when the tier launched, 46 on 2026-11-13) form
their own tier so they cannot flood the list. For them only: scrape everything, **keep, score and sync only senior titles**.

**Other tiers (added after this section was written).** `SENIOR_FILTER_TIERS` in
`run.py` now holds three tiers: Large MedTech, **Large Biotech** (added 2026-10-30)
and **Large Misc** (2026-11-11). All three get the same `SENIOR_TITLE_RE` filter
and the same Workday senior search terms, so everything below about the filter
applies to each of them.

Putting a tier behind the filter *after* its companies were scraped unfiltered
strands their non-senior rows in Notion: those URLs vanish from every run, the
company scrapes OK, and the close pass queues them all at once. One run wanted to
close 4,557 of 9,787 open rows (46.6%) and the 25% valve aborted it, correctly,
since it cannot tell a deliberate filter change from a systemic failure. Two
one-off scripts clean up deliberately instead, both dry-run by default:
`close_nonsenior_tiered_postings.py` closes open non-senior rows at tiered
companies (never a row with an Application Status or Date Applied, never one whose
Company relation does not resolve; stamps `Last Seen` per invariant 14), and
`clear_new_flag_nonsenior_biotech.py` only unticks `New This Run` on the same rows.
Adding a fourth tier means adding it to `SENIOR_FILTER_TIERS` and then running
the close script, in that order.

- **Filter:** `SENIOR_TITLE_RE` in `run.py` (named constant, edit it there):
  vp / svp / vice president / director / head of / general manager / chief. Runs in
  `prepare_jobs()`, which logs "kept X of Y (senior filter)" per company. Known
  noise: "Executive Assistant, VP …" and "Associate Director" pass.
- **Scorer:** `score_jobs._tier_line()` adds "Company tier: Large MedTech (>$1B
  revenue)" to each tiered job's entry in the **per-job message only**. The system
  prompt, fingerprint and cache key (job URL) are untouched, and untiered jobs'
  messages are byte-identical, so cached scores stay valid. Changing that wording
  needs a `SCORER_VERSION` bump. Why it exists: the rubric's must-have is
  $10M-$150M revenue, so a VP at a $30B company would score 3-4 regardless.
- **`URL_BASED_PLATFORMS`** (`run.py`) lists platforms whose scraper is
  `fetch_jobs(company_name, careers_url)`. `run_scrapers` hands them the saved
  Careers URL; there is no guessed-slug fallback and no URL means `No Careers URL`.
  Workday predates this and keeps its own branch. **`PLATFORM_ALIASES`** adds
  spellings `resolve_scraper()` should accept ("Oracle Cloud HCM", "SAP
  SuccessFactors"). "Oracle Taleo" is deliberately not an alias of Oracle Recruiting
  Cloud; no Taleo scraper exists.
- **Shared rules** (`scrapers/common.py`): one query per term in
  `SENIOR_SEARCH_TERMS` instead of paging a whole board (for a tiered company only;
  an untiered Oracle/SuccessFactors company gets `ALL_POSTINGS_TERMS`, an empty
  keyword, i.e. the whole board, and a board longer than `MAX_PAGES` raises), 0.5 s between requests, a
  normal User-Agent, dedupe by URL, `ScrapeShapeError` (invariant 25).
- **Workday** (existing scraper): gains `search_terms=` (used for the tier) and a
  shape check. Job URLs keep the `/en-US/{site}` form, since a URL is a posting's
  identity (invariant 22). Search is full-text, so a term over-fetches (Medtronic:
  984 postings for 5 terms, 21 kept); the regex does the real filtering.
  Failure modes: saved URL not a `myworkdayjobs.com` board; shared tenants (Masimo
  sits on Danaher's) need a facet.
- **Oracle Recruiting Cloud** (`scrapers/oracle_rc.py`): `GET
  /hcmRestApi/resources/latest/recruitingCEJobRequisitions` with `onlyData=true`,
  `expand=requisitionList.secondaryLocations` (**required**, or no list comes back)
  and `finder=findReqs;siteNumber=…,limit=25,offset=…,keyword=<term>,sortBy=POSTING_DATES_DESC`.
  siteNumber is the last segment of the saved URL. Failure modes: URL not
  `…/hcmUI/CandidateExperience/<lang>/sites/<site>`; reply without a requisition list.
- **SuccessFactors** (`scrapers/successfactors.py`): the server-rendered RMK page
  `GET /search/?q=<term>&startrow=<n>`, `tr.data-row`, 25 per page, total from
  `.paginationLabel`. Failure modes, all raised: JS-rendered shells (B. Braun),
  non-RMK sites (Steris, Straumann), and `career{N}.successfactors.com/career?company=`
  pages, which raise "needs a browser" without a request.
- **iCIMS, Avature, Taleo: not built.** No Large MedTech company needed them (see
  invariant 26). Add the name to `URL_BASED_PLATFORMS` when a scraper lands.
- `add_large_medtech_companies.py` created the rows; `restamp_score_cache.py` keeps
  existing scores valid across a narrow rubric edit (dry-run by default).

## Conventions

- Standard library and `requests` first. Current deps: `requests`, `python-dotenv`,
  `openpyxl`, `beautifulsoup4`, `pytest`. Ask before adding another.
- **All HTTP goes through `http_client.py`.** One shared session with urllib3 retries
  on 429 and 5xx only, never sleeping on `Retry-After`. Never call `requests.get/post/patch`
  directly. `try_known_patterns()` uses the separate low-retry session on purpose:
  those are speculative slug guesses where failing fast is correct.
- Print statements are the current logging. They are deliberately verbose and
  explain *why*, not just *what*. Match that tone if you add more.
- Docstrings in this codebase record the reasoning behind a decision, often
  including the bug that motivated it. Keep that style; it is the project's memory.
- Don't reformat or reflow files you aren't otherwise changing.

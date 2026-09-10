# CLAUDE.md

## Read this first: this is a public mirror

This repository is a **sanitized, public copy** of a private pipeline, kept as a
portfolio piece. It is *supposed* to differ from its private counterpart, and the
differences are load-bearing:

| File | How it differs | Why |
|---|---|---|
| `notion/client.py` | `os.environ["JOB_POSTINGS_DATABASE_ID"]`, no hardcoded fallback | the fallback upstream is a real Notion database id |
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

A pipeline that tracks job postings across ~425 target companies (medtech, digital
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
5. `run_scrapers()` — Greenhouse/Lever/Ashby/Workday via their public APIs; everything
   else marked HTML gets `scrapers/html_generic.py`. Returns `(jobs, scraped_ok, statuses)`.
   Workday takes its own branch: its board needs four parameters rather than one slug,
   and it deliberately has no guess-from-the-company-name fallback.
6. `sync_scrape_status(statuses)` — stamps every Target List row with how its scrape
   went. Anything not `OK` is what the Needs Manual Check view shows.
7. `mark_new_postings()` — flags first-seen postings against `output/seen_jobs.json`.
8. `score_all()` — batched, cached, fingerprinted, Haiku, rubric as system prompt.
9. `sync_jobs_to_notion(jobs, scraped_ok)` — dedup by URL, create/update, close what's
   genuinely gone. **Delta-only**: every owned property is compared against what Notion
   already holds and only differences are written. A steady-state run writes ~100 rows,
   not ~3,200.
10. `build_workbook()` — five-tab Excel export.

## Invariants

These encode bugs that were already found and fixed. Breaking one reintroduces a
real failure, so preserve them unless the task explicitly says otherwise.

1. **An error is not an answer.** A search that *failed to run* (network, API outage,
   billing) must never write a "Not found" marker. It leaves the field untouched so a
   future run retries. Only a search that *ran and genuinely found nothing* writes a
   marker. This distinction is the entire retry-safety design.
2. **Never close a row for a company that didn't scrape successfully.** Absence from
   this run's results is not proof a posting is gone; it is equally consistent with a
   timeout. Only companies in `scraped_ok` may have rows closed, a row whose Company
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
10. **Anything that changes a score must change the fingerprint.** `_prompt_fingerprint()`
    hashes the rubric, the model id, the keyword list, and `SCORER_VERSION`. Change how
    scoring works without bumping one of those and the cache serves stale answers
    forever, silently.
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
    job whose scoring failed comes back with `score` and `routing` of `None`; the diff
    skips those rather than blanking a good value. Same convention as
    `update_company_info()`.
16. **A company's page icon, not just `Website`, marks it as checked.** `Website` is a
    URL-type property, so it can't hold a text "Not found" marker the way `ATS Platform`
    does — writing empty means "never checked" (invariant 8) for every field except this
    one. `fill_missing_website()` stamps the page icon either way — a real favicon on
    success, the blank-square marker (`ats_finder.find_website.BLANK_ICON`) on a genuine
    "nothing found" — so `get_companies_missing_website()` filters on "no icon yet", not
    "no Website yet". A row a human iconed by hand for an unrelated reason will look
    already-checked and won't be retried; accepted trade-off, not a bug.

## Traps

- **`output/` is gitignored in full (except `.gitkeep`) but holds real state.** `score_cache.json` (~500 KB)
  represents money already spent. `seen_jobs.json` is run history. `notion_state.json`
  holds the previous run's date, which is what a closing posting's `Last Seen` gets
  stamped with. Never delete or regenerate these casually. Tests must stub
  `notion.state` rather than let it touch the real file — a test run that advances
  `last_run` to today makes the next real run stamp closing postings with the date of
  a test.
- **Any change to the rubric, the scoring prompt, the model, or the keyword list
  invalidates the entire cache** and triggers a full re-score of ~850 jobs. That is
  correct behavior and it costs real money. Say so before it happens.
- **`scoring/rubric.md` is the only rubric the code reads.** If you keep a second copy
  anywhere outside the repo — a doc, a wiki, a chat project — that copy is a read-only
  mirror of this file, not a second original: edit the repo copy and re-stamp the other
  from it. An edit to the copy changes no behavior.
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
(checkbox), Date Applied (date), Application Notes (text), Application Summary (formula).

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

`docs/punch-list.md` holds 20 prioritized improvements with fix-level detail, a status
line per item, and a suggested batching order at the bottom. Work one batch per branch.
`docs/project-status.md` has the longer history, including bugs already fixed and open
issues.

When asked to work on a numbered item, read that item in full before editing, and
check its cross-references — several items depend on another landing first. Update the
item's status line and the summary table when it lands.

## Conventions

- Standard library and `requests` first. Current deps: `requests`, `python-dotenv`,
  `openpyxl`, `beautifulsoup4`, `pytest`. Ask before adding another.
- **All HTTP goes through `http_client.py`.** One shared session with urllib3 retries
  on 429 and 5xx only, respecting `Retry-After`. Never call `requests.get/post/patch`
  directly. `try_known_patterns()` uses the separate low-retry session on purpose:
  those are speculative slug guesses where failing fast is correct.
- Print statements are the current logging. They are deliberately verbose and
  explain *why*, not just *what*. Match that tone if you add more.
- Docstrings in this codebase record the reasoning behind a decision, often
  including the bug that motivated it. Keep that style; it is the project's memory.
- Don't reformat or reflow files you aren't otherwise changing.

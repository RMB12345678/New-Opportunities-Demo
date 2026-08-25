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

**Never reconcile this repo against the private one.** If you are shown both and the
differences read like drift, they are not drift. "Restoring consistency" would publish
a private database id to a public repo.

Nothing in `output/` is ever committed here, and no real `.env` exists.

Guidance for Claude Code working in this repository.

## What this is

A pipeline that tracks job postings across ~336 target companies (medtech, digital
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
4. `run_scrapers()` — Greenhouse/Lever/Ashby via their public APIs; everything else
   marked HTML gets `scrapers/html_generic.py`. Returns `(jobs, skipped, scraped_ok)`.
5. `mark_new_postings()` — flags first-seen postings against `output/seen_jobs.json`.
6. `score_all()` — batched, cached, fingerprinted, Haiku, rubric as system prompt.
7. `sync_jobs_to_notion(jobs, scraped_ok)` — dedup by URL, create/update, close what's
   genuinely gone.
8. `build_workbook()` — five-tab Excel export.

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
   for fields they don't want touched; `update_company_info()` skips them.
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

## Traps

- **`output/` is gitignored in full (except `.gitkeep`) but holds real state.** `score_cache.json` (~500 KB)
  represents money already spent. `seen_jobs.json` is run history. Never delete or
  regenerate these casually.
- **Any change to the rubric, the scoring prompt, the model, or the keyword list
  invalidates the entire cache** and triggers a full re-score of ~850 jobs. That is
  correct behavior and it costs real money. Say so before it happens.
- **The rubric can drift** if you keep a second copy anywhere outside the repo.
  `scoring/rubric.md` is what the code reads, and nothing syncs it. See punch list #6.
- **`scoring/excluded_title_keywords.json` matches on substring**, deliberately. A new
  entry catches every title containing the word, management roles included.
- Repo is on Windows, Python 3.14, where stdout defaults to cp1252. `run.py`
  reconfigures stdout and stderr to UTF-8 with `errors="replace"` at import. Don't
  remove it — one curly quote in a job title would otherwise kill a run mid-pipeline.

## Notion schema

**Target List** (`NOTION_DATABASE_ID`): Company (title), Sector / Focus (text), HQ
(text), Source (text), ATS Platform (text), Scrape Method (text), Careers URL (**url**),
Jobs (relation → Job Postings).

**Job Postings** (`JOB_POSTINGS_DATABASE_ID`): Job Title (title), Company (relation →
Target List), Score (number), Routing (select: Scored / Needs Review / Non-fit),
Reasoning (text), Ambiguity Note (text), IC Role (checkbox), Location (text), URL (url),
ATS (text), First Seen (date), Last Seen (date), Still Open (checkbox), New This Run
(checkbox), Date Applied (date), Application Notes (text), Application Summary (formula).

Views on Job Postings: New This Run, Scored List, Needs Review, Non-fits, Applications.

## Current work

`docs/punch-list.md` holds 19 prioritized improvements with fix-level detail, a status
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

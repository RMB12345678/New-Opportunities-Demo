# Project status

*Originally written Aug 17–18, 2026. Ported into the repo and updated Aug 22, 2026.*

Background and history for anyone (including Claude) picking this up cold. For the
active work list see `docs/punch-list.md`. For working rules see `CLAUDE.md`.

## What this project is

An automated pipeline that tracks job postings across 331 target companies (medtech,
digital health, healthcare-focused PE/VC), scores them against a custom fit rubric,
and syncs results into a live Notion dashboard. The repo owner holds the architecture decisions
and business logic (the rubric); Claude wrote the implementation.

## Where everything lives

- **Code** — this repo, `github.com/RMB12345678/Job-Search-Agent`. Runs locally via
  `python run.py`. Not yet on a schedule.
- **Company data** — Notion "Target List" database, `<redacted>`
  (data source `<redacted>`). 331 companies.
- **Job data** — Notion "Job Postings" database, `<redacted>`
  (data source `<redacted>`). Related to Target List via a
  "Company" relation.
- **Dashboard** — Notion "Active Opportunities" page, `<redacted>`.
  Active Roles, To Do, Job Boards (ExecuNet, Reccy, Kidneyverse/Signals), and link
  cards to both databases.
- **Rubric** — `scoring/rubric.md` is what the code reads. A second copy lives in the
  Claude Project as "Scoring Rubric". They are not synced and have already diverged
  (the repo copy has "Head of Product", the Project copy does not). See punch list #6.
- **Profile doc** — "Profile details" in the Claude Project. Used for framing and
  pitch angle when writing outreach. Not read by the code.

## Cost controls already built in

- Haiku rather than Sonnet for scoring, batched and prompt-cached (though see punch
  list #8 — the caching may not actually be firing).
- Every "found nothing" outcome writes a persistent marker rather than leaving a
  blank, so a company is never re-searched once genuinely checked. Applies to ATS,
  Sector, HQ, and Careers URL independently.
- A search that *fails with an error* is treated differently from a search that *runs
  and finds nothing*. Errors leave the field untouched for retry. (Punch list #3 is a
  hole in this: a truncated model reply is neither, and currently lands in the
  permanent bucket.)
- Companies already marked "Not found" at the ATS level are excluded from the separate
  Careers URL search.

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
- Company-specific data corrections: Merge Labs (wrong URL), Pattern Bioscience vs.
  Pattern Biosciences (name collision), Arctop (no ATS, LinkedIn only), Piramidal
  (Consider ATS, confirmed).

## Open issues not in the punch list

- **Arbital Health** — ATS field says "Greenhouse and/or Lever (ambiguous)". Defaults
  to Greenhouse and prints a warning every run. Unresolved.
- **~90 companies have no real ATS** and depend on the generic HTML scraper, which
  cannot see JavaScript-rendered job lists. Punch list #5 covers the code side; the
  headless-browser question is still open.
- **Genuinely ambiguous company names** — Signals Group, BioHope Scientific, Montagu,
  Ortivity. No confirmed ATS or careers page. Not necessarily wrong, just unresolved.
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

- **Aug 22, 2026** — Full code review against commit `243b2f2`; produced
  `docs/punch-list.md` (17 items). Added `CLAUDE.md`. Ported this doc into the repo.
- **Aug 20, 2026** — Pushed to GitHub as `Job-Search-Agent` (initial commit).
- **Aug 17–18, 2026** — Added Head of Product to the rubric. Added Date Applied,
  Application Notes, Application Summary to Job Postings. Fixed the bug list above.

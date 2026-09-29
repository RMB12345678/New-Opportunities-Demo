# New Opportunities Demo

An automated pipeline that tracks job postings across a curated list of
target companies, scores each posting against a custom fit rubric using
Claude, and syncs everything into a live Notion dashboard.

Built by directing Claude (Anthropic's AI model) to architect, write, and
iteratively debug the implementation — the architecture decisions, cost
optimizations, and business logic (the rubric) are mine; Claude wrote and
fixed the code under direction.

*Note: this is a public demo version of a private repo that runs against
my own live target company list. The scoring rubric is my actual
criteria; the keyword filters and code architecture are the same as the
private version. Only the real company/application data has been
removed — that stays in a private database, not committed here.*

## What it does

1. **Researches companies automatically.** Given just a company name, it
   looks up which Applicant Tracking System (ATS) the company uses,
   their sector, HQ, and careers page URL — flagging ambiguous name
   collisions rather than guessing.
2. **Scrapes job postings** — using real public APIs for companies on
   Greenhouse, Lever, Ashby, Workday, Workable, Personio, Breezy,
   Rippling, Teamtailor, SmartRecruiters or Pinpoint, and a best-effort
   generic scraper for companies with in-house career pages. A page that
   only *looks* in-house is checked first for an ATS board it embeds,
   redirects to or links — but only when the page names exactly one, so a
   careers page listing a portfolio of other companies' boards is refused
   rather than guessed at.
3. **Scores every posting** against a rubric (`scoring/rubric.md`) using
   Claude — the posting's own text, not just its title — batched and
   cached to keep API costs low. The cache is fingerprinted against the
   rubric, so editing the rubric re-scores everything instead of serving
   stale answers.
4. **Syncs results to Notion**, deduplicated by URL, with dedicated views
   for new postings, scored matches, non-fits, applications sent, and the
   full list. A posting is only marked closed when its company actually
   scraped — a timeout never retires a live job, and the sync writes only
   the properties that actually changed: a steady-state run touches around
   a hundred rows rather than every row it can see.
5. **Rehearses before it spends.** `--dry-run` performs every read and
   scrape but skips all writes and all paid calls, printing what it would
   have done.

## How it works

```mermaid
flowchart TD
    Start([python run.py]) --> S1

    S1["Fill missing ATS Platform\nfull profile lookup: sector, HQ, ATS, careers URL\nAmbiguous name collisions flagged, not guessed"] --> S2
    S2["Backfill missing Sector/HQ\nfor companies that have an ATS but\nare still missing profile fields"] --> S3
    S3["Backfill missing Careers URL\nIf ATS already confirmed: search targets\nTHAT platform's URL pattern specifically\nFallback chain: dedicated page -> homepage -> Google search"] --> S4

    S4{"Scrape\nATS Platform?"}
    S4 -->|One of eleven API-backed platforms| S4a["Hit real public API\nUse CONFIRMED slug from saved URL first,\nfall back to guessing from company name"]
    S4 -->|HTML / in-house page| S4b["Look for an embedded ATS board first\n(redirect, embed or link — and only if the\npage names exactly one board)\nelse generic scraper: job-title-like link\ntext, filters nav/footer noise"]
    S4 -->|No ATS found / Ambiguous| S4c["Skipped -> Needs Manual Check"]

    S4a --> S4d
    S4b --> S4d

    S4d["Collapse postings sharing a URL\nthe URL is a posting's identity, and\nNotion holds one row per URL"] --> S5

    S5["Flag new postings\n'new' is based on Notion's own\ncreate-vs-update check"] --> S6

    S6["Score against rubric"]
    S6 --> S6a{"Title matches an\nexcluded keyword?"}
    S6a -->|Yes| S6b["Auto score 0 / Non-fit\nNO API call — free"]
    S6a -->|No| S6c{"Already in\nscore_cache.json\nwith a real score\nAND a matching rubric fingerprint?"}
    S6c -->|Yes| S6d["Skip — use cached score"]
    S6c -->|No| S6e["Batch 5 jobs/call to a fast model\nTitle AND posting text sent\nrubric text prompt-cached\nRouting enforced in code,\nnot trusted from model output"]
    S6b --> S7
    S6d --> S7
    S6e --> S7

    S7["Sync to Notion\nDedup by URL; write only what changed\nClose only rows whose company scraped OK\nAbort the close pass if >25% would close\nClear 'New This Run' on every row\nthis run did not create\nRetries with backoff on transient failures"] --> S8
    S8["Excel export\nfive tabs: the four scoring views\nplus Needs Manual Check"] --> End([Done])

    style S4a fill:#d4edda
    style S4b fill:#fff3cd
    style S4c fill:#f8d7da
    style S6b fill:#d4edda
```

## Architecture highlights

- **Cost-optimized scoring**: batches jobs per API call with the rubric
  prompt-cached, and routes high-volume scoring to a cheaper, faster
  model (Haiku) while reserving a stronger model (Sonnet, with web
  search) for the harder company-research judgment calls — far cheaper
  than a naive one-call-per-job approach.
- **A cache that knows when it's wrong**: cached scores are stamped with
  a fingerprint over the rubric text, the model id, the excluded-keyword
  list, and a version constant. Change any input to a score and the
  affected entries are re-scored automatically. Without this, editing the
  rubric silently changed nothing.
- **Absence is not proof of deletion**: a posting missing from a run is
  equally consistent with a scraper timeout, so rows are only closed for
  companies that scraped successfully — and a run that would close more
  than 25% of open rows aborts the close pass entirely rather than
  retiring a live board on one bad afternoon.
- **Writes only what changed**: every property the pipeline owns is
  compared against what Notion already holds, and a row with nothing new
  to say costs zero writes. Getting that right is subtler than it looks —
  a comparison that can never be equal (Notion returns its own casing for
  a select, `7.0` for a `7`, `None` for an empty text field) rewrites its
  row on every run while the sync still reports itself healthy. That is
  why the dry run prints *why* each write is planned, not just that it is.
- **A URL is a posting's identity**: one row per URL, and one write per
  URL per run. A board that lists a multi-location role once per city
  used to produce one Notion row per city, and since the sync can only
  keep one row per URL up to date, the extras became unreachable — stuck
  open and flagged new forever. Repeats are now collapsed at the source,
  again across the whole run, and once more inside the sync itself.
- **Persistent negative caching**: every "checked, nothing found" result
  writes a permanent marker, so a company is never re-researched (and
  re-paid for) once genuinely checked. A search that *fails* due to an
  error is treated differently from one that *succeeds and finds
  nothing* — errors retry next run instead of writing a false negative.
- **Resilient sync**: all HTTP goes through one shared session
  (`http_client.py`) that retries with growing backoff on 429s and 5xxs
  only, respecting `Retry-After`. One failed write no longer takes down a
  run. Speculative ATS slug guesses deliberately use a separate
  fail-fast session, where retrying a wrong guess just wastes time.
- **Free pre-filtering**: job titles matching an excluded-keyword list
  get an instant "Non-fit" with zero API cost, before ever reaching the
  scoring step.

## Setup

```bash
git clone https://github.com/<your-username>/new-opportunities-demo.git
cd new-opportunities-demo
pip install -r requirements.txt
cp .env.example .env
# fill in .env with your own Notion integration token, database IDs,
# and Anthropic API key — see comments in .env.example
```

Then export the variables (or use `python-dotenv`, already handled
automatically by `run.py`) and run:

```bash
python run.py --dry-run   # rehearsal: no writes, no API spend
python run.py             # the real thing
python -m pytest tests/   # offline checks, no network or keys needed
python output_excel.py    # rebuild the workbook from existing output/
```

`--dry-run` skips every Notion write and every Anthropic call and prints
what it would have done. It still performs read-only Notion queries and
still scrapes public ATS endpoints, so it is a rehearsal, not an offline
mode.

### `output/` ships empty on purpose

A fresh clone has an empty `output/` holding only a `.gitkeep`, and that is
the intended state — nothing is missing. Everything the pipeline writes lands
there and none of it is committed: `jobs.json` and `skipped_companies.json`
from the scrape, `score_cache.json` (the scores already paid for),
`seen_jobs.json` (run history), and the generated `.xlsx` workbook. The
directory fills itself on your first run.

That also means `python output_excel.py` has nothing to rebuild from until
you have run the pipeline at least once.

## Notion setup

You'll need two Notion databases:

**Target List** — Company (title), Sector / Focus (text), HQ (text),
Source (text), ATS Platform (text), Scrape Method (text), Careers URL
(URL type), Website (URL type), Jobs (relation → Job Postings), Scrape
Status (select: OK / Failed / No Scraper / No Careers URL), Scrape Note
(text), Failing Since (date)

Those last three are a stamp the pipeline computes each run, and the page
icon is pipeline-owned too — a favicon where the company's domain
resolves, a blank square where it genuinely doesn't. A "Needs Manual
Check" view filtered on `Scrape Status is not OK` and sorted by
`Failing Since` ascending puts the oldest breakage first.

**Job Postings** — Job Title (title), Company (relation → Target List),
Score (number), Routing (select: Scored / Needs Review / Non-fit),
Reasoning (text), Ambiguity Note (text), IC Role (checkbox), Location
(text), URL (url), ATS (text), First Seen / Last Seen (date), Still Open
(checkbox), New This Run (checkbox), Application Status (select), Date
Applied (date), Application Notes (text), Application Summary (formula)

Views on Job Postings: Scored List, New This Run, Applications,
Non-fits, and a Total list. There is deliberately no Needs Review view —
"Needs Review" is a `Routing` option, so it shows as a column and those
postings turn up in New This Run anyway.

`Application Status` and `Application Notes` are yours to set by hand;
the pipeline only reads them. The one thing it does with them is stamp
today's date on `Date Applied` when it finds a row marked Applied with
the date still blank — the free substitute for a Notion automation that
would need a paid plan.

`Careers URL` and `URL` must be Notion **URL**-type properties, not rich
text — Notion rejects an empty string on a URL property, so empty is
written as null.

Create a Notion internal integration at notion.so/my-integrations, share
it with both databases via each database's "Connections" menu, and put
the token and database IDs in `.env`.

## Customizing the rubric

Edit `scoring/rubric.md` directly — it's sent as-is as the scoring
system prompt, so any plain-English scoring criteria works. No code
changes needed.

## Extending to more ATS platforms

Each API-backed scraper lives in `scrapers/<platform>.py` and exposes a
`fetch_jobs(company_name)` function. To add a new platform, add a file
here, register it in the `SCRAPERS` dict in `run.py`, and optionally add
its URL pattern to `KNOWN_PATTERNS` in `ats_finder/find_ats.py` so it's
detected automatically.

## Tests

```bash
python -m pytest tests/
```

The suite is offline — no network, no API keys, no Notion, and it stubs
the on-disk run state so a test run can never advance it. It covers the
close-pass rules that decide whether a posting is retired (company-relation
resolution, the scraped-successfully guard, the 25% ratio valve, its 20-row
floor, the empty-database case), the delta sync and the comparison traps
that would otherwise rewrite every row on every run, the one-row-per-URL
rule and the "New This Run" reset, each ATS scraper's slug extraction and
job-dict contract, embedded-board detection and its refusal to guess when a
page names several boards, and the checks that tell a never-researched field
apart from one holding a wrong value.

## License

MIT

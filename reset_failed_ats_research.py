"""
One-off utility: clears `ATS Platform` on every Companies row reading
"Not found (auto-search failed)", so the next `python run.py` researches
them again from scratch.

This is step 5 of punch list #3, and it only makes sense now that steps 1-4
have landed. Those rows are the wreckage of the bug: `find_via_search()`
asked for a full company profile with `max_tokens: 500` and web search
enabled, and nothing checked whether the reply finished. A model that
narrated its searching before answering ran out of room mid-block, the
HTTP call still returned 200, every `_extract()` fell through to its
default, and "Not found (auto-search failed)" went into Notion as a
permanent marker.

Permanent is the problem. Invariant 8 makes a non-blank value mean
"already researched", so those companies are never re-researched — and
`get_companies_missing_url()` additionally excludes any row whose ATS
contains "Not found", so they lose the careers-URL backfill too. A company
left the pipeline entirely on the strength of a reply that never finished.

Re-running them is only safe because steps 1-4 landed first: a truncated
reply now raises instead of writing a marker, so a row cleared here cannot
be re-condemned by the same bug. Running this against the old code would
just re-create the markers, at full API price.

Run this ONCE, after steps 1-4:
    python reset_failed_ats_research.py --dry-run   # see what would change
    python reset_failed_ats_research.py             # do it

Then a normal `python run.py`. Every cleared row is blank, so
`fill_missing_ats()` treats it as a new company and does a full profile
lookup — which costs one Anthropic call each. That is the point, and it is
the price of the bug: the count is printed before anything is written so
the spend is a decision rather than a surprise.

Deliberately narrow. Only the exact auto-search-failure marker is cleared.
Other "Not found" values ("Not found (no careers page located)", and the
rest) are genuine research answers from replies that finished, and clearing
those would be paying to re-derive something already known — which is the
waste invariant 8 exists to prevent.
"""
import argparse

from dotenv import load_dotenv
load_dotenv()

from notion.client import get_all_companies, update_company_info

# The exact string find_via_search() used to write when a reply came back
# unparseable. Matched with `in` rather than `==` because some rows carry a
# trailing note appended by hand, but anchored on the parenthetical so it
# cannot catch a genuine "Not found (no careers page located)".
MARKER = "Not found (auto-search failed)"

# Safety valve, same spirit as the close pass's 25% guard: if this ever
# matches most of the database, the marker string has drifted and the right
# move is to stop rather than blank hundreds of researched rows.
MAX_SHARE = 0.25


def main():
    parser = argparse.ArgumentParser(
        description="Clear the auto-search-failure marker so those companies get re-researched.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be cleared without writing anything.")
    args = parser.parse_args()

    companies = get_all_companies()
    to_reset = [c for c in companies if c["ats_platform"] and MARKER in c["ats_platform"]]

    if not to_reset:
        print(f"No company carries the {MARKER!r} marker. Nothing to do.")
        return

    share = len(to_reset) / len(companies)
    if share > MAX_SHARE:
        print(f"ABORTING: {len(to_reset)} of {len(companies)} rows ({share:.0%}) matched, "
              f"more than the {MAX_SHARE:.0%} this expects. That points at a changed "
              f"marker string rather than a database full of failed searches. "
              f"Nothing was written.")
        raise SystemExit(1)

    verb = "would clear" if args.dry_run else "clearing"
    print(f"{len(to_reset)} row(s) carry the auto-search-failure marker, {verb} all of them.")
    print(f"A real run afterwards will spend roughly {len(to_reset)} Anthropic research "
          f"call(s) re-profiling them.\n")
    for row in to_reset:
        print(f"  -> {row['company']}")

    print()
    for row in to_reset:
        # "" clears a rich_text field; Scrape Method goes too, so the row
        # is indistinguishable from a never-researched one and takes the
        # same path through fill_missing_ats(). Careers URL is left alone:
        # if one was saved it is still the best lead we have, and the
        # research pass overwrites it when it finds better.
        update_company_info(row["page_id"], ats_platform="", scrape_method="",
                            dry_run=args.dry_run)

    if args.dry_run:
        print(f"[dry-run] Nothing was written. {len(to_reset)} row(s) would be cleared.")
    else:
        print(f"Done. Cleared ATS Platform and Scrape Method on {len(to_reset)} row(s).")
        print("Run `python run.py` now — fill_missing_ats() will re-profile each of them.")


if __name__ == "__main__":
    main()

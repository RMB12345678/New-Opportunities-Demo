"""
One-off utility: clears `Careers URL` on every Companies row whose saved
URL points at a Notion page rather than a careers page, so the Careers URL
backfill in `run.py` researches a real one on the next run.

Why this exists: 20 rows share one identical `app.notion.com/p/PocDoc-...`
link in their `Careers URL` — a paste that went into the wrong column at
some point. The pipeline has no way to notice. `get_companies_missing_url()`
selects on `not row["careers_url"]`, i.e. a BLANK field, because invariant 8
makes blank mean "never researched". A field holding the *wrong* value is
not blank, so those rows are never revisited: the scrape retries them every
run, falls back to guessing a slug from the company name, fails, stamps
Needs Manual Check, and the backfill that would fix it skips them. Forever.

That loop is what this breaks. It clears exactly one property and only on
rows that match the placeholder, so a row whose Careers URL is a real page
is never touched.

Run this ONCE:
    python reset_placeholder_careers_urls.py --dry-run   # see what would change
    python reset_placeholder_careers_urls.py             # do it

After running it, a normal `python run.py` will research a real Careers URL
for each cleared row.

**One caveat, deliberately not worked around here.** Clearing the URL is
enough only for rows whose `ATS Platform` names something real.
`get_companies_missing_url()` also excludes any row whose ATS Platform
contains "Not found", "AMBIGUOUS" or "N/A" — a conclusive answer about the
company as a whole, so re-searching just its URL would be waste. 11 of the
20 are PE/VC firms in exactly that state. Clearing their URL still removes
a wrong link from the dashboard, but it will NOT trigger re-research; that
needs their ATS marker cleared too, which is a full profile lookup per
company and real Anthropic spend. This script does not decide that for you
— it prints which rows are in that state so the choice stays yours.
"""
import argparse
from urllib.parse import urlparse

from dotenv import load_dotenv
load_dotenv()

from notion.client import get_all_companies, update_company_info

# Hosts that are never a careers page. Matched on the parsed host rather
# than a substring so a real careers page that merely mentions "notion"
# somewhere in its path (notionhealth.com/careers, say) is not caught.
PLACEHOLDER_HOSTS = {"notion.com", "notion.so"}

# Markers that make get_companies_missing_url() skip a row regardless of
# whether its Careers URL is blank — kept in sync with that function.
CONCLUSIVE_ATS_MARKERS = ("Not found", "AMBIGUOUS", "N/A")

# Safety valve, in the spirit of the close pass's 25% guard. If this ever
# matches a large share of the database, the host check has gone wrong and
# the right move is to stop rather than blank hundreds of real URLs.
MAX_SHARE = 0.10


def is_placeholder(url):
    if not url:
        return False
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in PLACEHOLDER_HOSTS)


def main():
    parser = argparse.ArgumentParser(
        description="Clear Careers URLs that point at a Notion page instead of a careers page.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be cleared without writing anything.")
    args = parser.parse_args()

    companies = get_all_companies()
    to_clear = [c for c in companies if is_placeholder(c["careers_url"])]

    if not to_clear:
        print("No Careers URL points at a Notion page. Nothing to do.")
        return

    share = len(to_clear) / len(companies)
    if share > MAX_SHARE:
        print(f"ABORTING: {len(to_clear)} of {len(companies)} rows ({share:.0%}) matched, "
              f"which is more than the {MAX_SHARE:.0%} this expects. That points at a bug "
              f"in the host check, not at a database full of placeholder URLs. "
              f"Nothing was written.")
        raise SystemExit(1)

    researchable, blocked = [], []
    for row in to_clear:
        ats = row["ats_platform"] or ""
        (blocked if any(m in ats for m in CONCLUSIVE_ATS_MARKERS) else researchable).append(row)

    verb = "would clear" if args.dry_run else "clearing"
    print(f"{len(to_clear)} row(s) with a placeholder Careers URL, {verb} all of them.\n")

    print(f"  {len(researchable)} will get a real URL researched on the next run:")
    for row in researchable:
        print(f"    -> {row['company']}  (ATS: {row['ats_platform']})")

    if blocked:
        print(f"\n  {len(blocked)} will have the wrong link removed but will NOT be "
              f"re-researched — their ATS Platform is a conclusive marker, which "
              f"get_companies_missing_url() excludes:")
        for row in blocked:
            print(f"    -> {row['company']}  (ATS: {row['ats_platform']})")
        print("    Clearing ATS Platform on these too would re-open them, at the cost "
              "of one full profile lookup each. Not done here — see the module docstring.")

    print()
    for row in to_clear:
        # "" (not None) is what clears a Notion URL property: None means
        # "don't touch this field" to update_company_info, and Notion
        # rejects the empty string, so it maps "" -> {"url": None}.
        update_company_info(row["page_id"], careers_url="", dry_run=args.dry_run)

    if args.dry_run:
        print(f"[dry-run] Nothing was written. {len(to_clear)} row(s) would be cleared.")
    else:
        print(f"Done. Cleared Careers URL on {len(to_clear)} row(s).")
        print("Run `python run.py` now — the Careers URL backfill will research a real "
              "one for each of the rows listed as researchable above.")


if __name__ == "__main__":
    main()

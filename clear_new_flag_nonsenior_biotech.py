"""One-off: untick New This Run on Large Biotech postings that fail the senior filter.

Why this exists. The Large Biotech tier was added after some of its companies had
already been scraped unfiltered, so their non-senior postings sit in the New This
Run view. This applies the same rule run.py's prepare_jobs() applies going forward
(SENIOR_TITLE_RE) to rows already in Notion, and clears only the New This Run box.

It does NOT close, archive or delete anything, and touches no other property. The
rows stay in Job Postings; they just stop appearing as new. Dry-run by default.

    python clear_new_flag_nonsenior_biotech.py           # report only
    python clear_new_flag_nonsenior_biotech.py --apply   # untick the box
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import argparse

from dotenv import load_dotenv
load_dotenv()

from notion import client as nc
from run import LARGE_BIOTECH_TIER, SENIOR_TITLE_RE


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true",
                        help="actually untick; without it nothing is written")
    args = parser.parse_args()

    companies = nc.get_all_companies()
    biotech_ids = {c["page_id"]: c["company"] for c in companies
                   if c.get("tier") == LARGE_BIOTECH_TIER}
    print(f"{len(biotech_ids)} companies tiered {LARGE_BIOTECH_TIER!r}")

    rows = nc._query_all_job_postings({c["page_id"]: c["company"] for c in companies})
    flagged = [r for r in rows
               if r["new_this_run"] and r["company_page_id"] in biotech_ids]
    to_clear = [r for r in flagged if not SENIOR_TITLE_RE.search(r.get("title") or "")]
    print(f"{len(flagged)} Large Biotech rows have New This Run set; "
          f"{len(flagged) - len(to_clear)} pass the senior filter and stay, "
          f"{len(to_clear)} fail it")
    for r in to_clear:
        print(f"    {r['company']}: {(r.get('title') or '')[:70]}")

    if not args.apply:
        print("\nDry run: nothing written. Re-run with --apply to untick them.")
        return
    for i, r in enumerate(to_clear, 1):
        nc._update_job_posting(r["page_id"], {"New This Run": "__NO__"})
        if i % 100 == 0:
            print(f"  cleared {i}/{len(to_clear)}")
    print(f"Cleared New This Run on {len(to_clear)} rows.")


if __name__ == "__main__":
    main()

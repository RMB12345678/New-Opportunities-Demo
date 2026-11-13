"""Archive closed Job Postings rows first seen more than six months ago.

Why this exists. Job Postings only grows: the close pass flips Still Open off
but never removes a row, so every posting ever seen stays in the database and
in every full read. This trims the long tail. The reads are sliced (invariant
26), so the 10,000-row query limit is no longer the reason; it is housekeeping
and keeps the Total list and the full-table reads a size a person can use.

What is kept, whatever its age: anything still open, anything with an
Application Status or Date Applied (your application history), rows with no
URL (added by hand), and rows with a blank First Seen (age unknown). The rules
live in notion.client.select_archivable_postings() and are tested.

"Archive" here is Notion's trash: restorable from Notion for 30 days, gone
after. That is why this is dry-run by default.

    python archive_old_postings.py           # report only, writes nothing
    python archive_old_postings.py --apply   # archive them
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import argparse
import datetime

from dotenv import load_dotenv
load_dotenv()

from notion import client as nc


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true",
                        help="actually archive; without it nothing is written")
    args = parser.parse_args()

    today = datetime.date.today()
    rows = nc._query_all_job_postings()
    to_archive, kept = nc.select_archivable_postings(rows, today)

    cutoff = today - datetime.timedelta(days=nc.ARCHIVE_AFTER_DAYS)
    print(f"{len(rows)} Job Postings rows; cutoff First Seen < {cutoff}")
    print(f"  would archive: {len(to_archive)}")
    for reason, n in kept.most_common():
        print(f"  kept — {reason}: {n}")
    for row in to_archive[:10]:
        print(f"    e.g. {row['first_seen'][:10]}  {(row.get('title') or '')[:60]}")

    if not args.apply:
        print("\nDry run: nothing written. Re-run with --apply to archive.")
        return
    for i, row in enumerate(to_archive, 1):
        nc._archive_job_posting(row["page_id"])
        if i % 100 == 0:
            print(f"  archived {i}/{len(to_archive)}")
    print(f"Archived {len(to_archive)} rows (restorable from Notion's trash for 30 days).")


if __name__ == "__main__":
    main()

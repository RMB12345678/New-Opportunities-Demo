"""One-off: close open postings at senior-filter-tier companies that fail the senior filter.

Why this exists. The Large Biotech tier was put behind the senior-title filter
after its companies had already been scraped unfiltered. prepare_jobs() now drops
the non-senior postings before the sync, so those rows' URLs are absent from every
run, their company scrapes OK, and the close pass queues them all at once: one run
wanted to close 4557 of 9787 open rows (46.6%) and the 25% safety valve aborted
it. The valve was right to; it cannot tell a deliberate filter change from a
systemic failure, so this closes them deliberately instead, leaving the valve
untouched for the failures it exists to catch.

Scope is deliberately narrow, in the spirit of invariant 2:
  - only companies whose tier is in run.SENIOR_FILTER_TIERS
  - only rows that are open, have a URL, and whose title fails SENIOR_TITLE_RE
  - never a row with an Application Status or Date Applied (set by hand by the owner)
  - never a row whose Company relation does not resolve

It sets Still Open off and Last Seen to the previous run's date (invariant 14),
and clears New This Run if set. Nothing is deleted. Dry-run by default.

    python close_nonsenior_tiered_postings.py           # report only
    python close_nonsenior_tiered_postings.py --apply   # close them
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import argparse
from collections import Counter
from datetime import date

from dotenv import load_dotenv
load_dotenv()

from notion import client as nc
from notion import state
from run import SENIOR_FILTER_TIERS, SENIOR_TITLE_RE


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true",
                        help="actually close; without it nothing is written")
    args = parser.parse_args()

    companies = nc.get_all_companies()
    tiered_ids = {c["page_id"]: c["company"] for c in companies
                  if c.get("tier") in SENIOR_FILTER_TIERS}
    print(f"{len(tiered_ids)} companies in tiers {sorted(SENIOR_FILTER_TIERS)}")

    rows = nc._query_all_job_postings({c["page_id"]: c["company"] for c in companies})
    open_tiered = [r for r in rows
                   if r["still_open"] and r.get("url")
                   and r["company_page_id"] in tiered_ids]
    non_senior = [r for r in open_tiered
                  if not SENIOR_TITLE_RE.search(r.get("title") or "")]
    protected = [r for r in non_senior
                 if r.get("application_status") or r.get("date_applied")]
    to_close = [r for r in non_senior if r not in protected]

    print(f"{len(open_tiered)} open tiered rows; {len(open_tiered) - len(non_senior)} "
          f"pass the senior filter and stay")
    print(f"{len(protected)} fail it but carry an Application Status / Date Applied "
          f"and are left open")
    print(f"{len(to_close)} to close, by company:")
    for name, n in Counter(r["company"] for r in to_close).most_common():
        print(f"    {n:5d}  {name}")

    # Same date the close pass uses: the run that closes a posting is the run
    # that did not see it, so the previous run is the last that did.
    last_seen = state.load().get("last_run") or date.today().isoformat()

    if not args.apply:
        print(f"\nDry run: nothing written. --apply would stamp Last Seen {last_seen}.")
        return
    for i, r in enumerate(to_close, 1):
        properties = {"Still Open": "__NO__", "date:Last Seen:start": last_seen}
        if r.get("new_this_run"):
            properties["New This Run"] = "__NO__"
        try:
            nc._update_job_posting(r["page_id"], properties)
        except Exception as e:
            print(f"     [failed] {r['company']} — {r.get('title')}: {e}")
        if i % 100 == 0:
            print(f"  closed {i}/{len(to_close)}")
    print(f"Closed {len(to_close)} rows.")


if __name__ == "__main__":
    main()

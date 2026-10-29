"""
One-off utility: clears the "N/A — PE/VC firm, not a job-pipeline scrape
target" marker from every PE/VC row in the Notion database, so the next
`python run.py` treats them as new companies and does a full auto-fill +
scrape attempt on them — the same as any startup.

Why this exists: PE/VC firms were originally excluded on the assumption
they hire mostly through recruiters/networks rather than public job
boards. That assumption doesn't hold up against the rubric itself, which
explicitly lists "Operating Partner, Commercial Advisor, Portfolio
Executive (PE/VC context only)" as target titles — exactly the kind of
role a firm's own careers page might list. This script reverses that
earlier exclusion.

Run this ONCE:
    python reset_pe_vc_firms.py

After running it, do a normal `python run.py` — it'll auto-fill ATS/HQ/
sector for every PE/VC firm and attempt to scrape them like any other
company. Some genuinely won't have public job boards (that's fine, they'll
just land in Needs Manual Check like any other company with no ATS found)
but this at least gives every firm a fair, individual look instead of a
blanket assumption.
"""
from dotenv import load_dotenv
load_dotenv()

from notion.client import get_all_companies, update_company_info

MARKER = "N/A — PE/VC firm, not a job-pipeline scrape target"


def main():
    companies = get_all_companies()
    to_reset = [c for c in companies if c["ats_platform"] == MARKER]

    if not to_reset:
        print("No companies found with the PE/VC exclusion marker. Nothing to do.")
        return

    print(f"Found {len(to_reset)} PE/VC firm(s) to re-open for scraping:")
    for row in to_reset:
        print(f"  -> {row['company']}")
        update_company_info(row["page_id"], ats_platform="", scrape_method="")

    print(f"\nDone. Cleared ATS Platform on {len(to_reset)} companies.")
    print("Run `python run.py` now — it will auto-fill and attempt to scrape all of them.")


if __name__ == "__main__":
    main()

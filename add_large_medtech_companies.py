"""One-off: add the 37 Large MedTech companies (>$1B revenue) to Companies.

Each new row gets ONLY its name and Company Tier = "Large MedTech". Every
other field is left blank on purpose — a blank ATS Platform is the signal
fill_missing_ats() researches (sector, HQ, ATS, careers URL), so the
existing pipeline does the rest.

Skips any company already present, matched case-insensitively after
normalising punctuation and through an alias table for the obvious variants
("BD" vs "Becton Dickinson"). Existing rows are NEVER modified: this script
only creates. It reports which were skipped so a human can decide whether
those rows need the tier set by hand.

The database comes from NOTION_DATABASE_ID in .env; nothing is hardcoded.

    python add_large_medtech_companies.py --dry-run   # report only
    python add_large_medtech_companies.py             # create
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import argparse
import re
import time

from dotenv import load_dotenv
load_dotenv()

from http_client import session
from notion.client import (
    BASE_URL, HEADERS, NOTION_DATABASE_ID, get_all_companies, _raise_with_detail,
)

TIER = "Large MedTech"

COMPANIES = [
    "Medtronic", "Johnson & Johnson MedTech", "Abbott Laboratories",
    "Siemens Healthineers", "GE HealthCare", "Boston Scientific",
    "BD (Becton Dickinson)", "Philips", "Alcon", "B. Braun", "Terumo",
    "Edwards Lifesciences", "ResMed", "Steris", "Bausch + Lomb", "Dexcom",
    "Align Technology", "The Cooper Companies", "Coloplast", "Teleflex",
    "Sonova", "Insulet", "Envista", "Straumann", "Convatec", "Cochlear",
    "ICU Medical", "Enovis", "Carl Zeiss Meditec", "Masimo",
    "Fisher & Paykel Healthcare", "Integer Holdings", "Elekta",
    "Merit Medical", "LivaNova", "Penumbra", "Haemonetics",
]

# Other spellings an existing row might carry, normalised (see _norm). Kept
# short and obvious on purpose: a loose alias would skip a genuinely
# different company, and that failure is silent.
ALIASES = {
    "Johnson & Johnson MedTech": ["j&j medtech", "johnson johnson medtech", "jnj medtech",
                                  "johnson & johnson", "johnson johnson"],
    "Abbott Laboratories": ["abbott"],
    "BD (Becton Dickinson)": ["bd", "becton dickinson", "becton dickinson and company",
                              "bd becton dickinson"],
    "GE HealthCare": ["ge healthcare", "ge health care", "ge healthcare technologies"],
    "Bausch + Lomb": ["bausch lomb", "bausch and lomb"],
    "The Cooper Companies": ["cooper companies", "coopercompanies", "cooper cos"],
    "Fisher & Paykel Healthcare": ["fisher paykel healthcare", "fisher and paykel healthcare",
                                   "fisher paykel"],
    "Carl Zeiss Meditec": ["zeiss meditec", "carl zeiss meditec ag"],
    "Steris": ["steris plc"],
    "Boston Scientific": ["boston scientific corporation"],
}


def _norm(name):
    """Lowercase, '&'/'+' read as 'and', everything but letters/digits/spaces
    dropped, whitespace collapsed."""
    name = (name or "").lower().replace("&", " and ").replace("+", " and ")
    name = re.sub(r"[^a-z0-9 ]", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def _variants(name):
    # "and" is dropped from both sides so "Bausch + Lomb" == "Bausch & Lomb"
    # == "Bausch Lomb".
    forms = {_norm(name)} | {_norm(a) for a in ALIASES.get(name, [])}
    return {re.sub(r"\band\b ?", "", f).strip() for f in forms}


def existing_match(name, existing_by_norm):
    for v in _variants(name):
        key = re.sub(r"\band\b ?", "", v).strip()
        if key in existing_by_norm:
            return existing_by_norm[key]
    return None


def create_row(name):
    resp = session.post(f"{BASE_URL}/pages", headers=HEADERS, json={
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": {
            "Company": {"title": [{"text": {"content": name}}]},
            "Company Tier": {"select": {"name": TIER}},
        },
    })
    _raise_with_detail(resp)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rows = get_all_companies()
    existing_by_norm = {}
    for r in rows:
        key = re.sub(r"\band\b ?", "", _norm(r["company"])).strip()
        if key:
            existing_by_norm[key] = r["company"]
    print(f"{len(rows)} companies already in Notion")

    added, skipped = [], []
    for name in COMPANIES:
        hit = existing_match(name, existing_by_norm)
        if hit:
            print(f"  skip  {name}  (already present as '{hit}')")
            skipped.append((name, hit))
            continue
        if args.dry_run:
            print(f"  [dry-run] would add {name}")
        else:
            create_row(name)
            print(f"  added {name}")
            time.sleep(0.4)
        added.append(name)

    print(f"\n{len(added)} {'would be ' if args.dry_run else ''}added, {len(skipped)} skipped")
    for name, hit in skipped:
        print(f"  skipped: {name} -> existing '{hit}'")


if __name__ == "__main__":
    main()

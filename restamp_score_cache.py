"""One-off: keep existing cached scores valid after a deliberate rubric edit.

Why this exists. The score cache tags every entry with a fingerprint of the
rubric, model, keyword list and SCORER_VERSION (invariant 10), so ANY rubric
edit makes every entry look stale and the next run re-scores the whole cache.
When an edit is narrow, that is mostly money spent to re-confirm the same
answers. This rewrites each entry's `rubric_fp` to the current fingerprint so
`score_all()` treats it as current, and keeps the old one in `restamped_from`
so the cache still tells the truth about which rubric produced a score.

What it does NOT re-stamp: entries whose reasoning or ambiguity note mentions
the thing the edit changed (EXCLUDE_PATTERN below). Those were scored under a
rule that no longer exists, and they alone are left stale so the next run
re-scores them. Entries that never name the rule but were pushed down by it
cannot be found this way — that is the accepted limitation.

Dry-run by default. --apply backs up output/score_cache.json first (the cache
is money already spent, so it is never rewritten without a copy beside it).

    python restamp_score_cache.py            # report only
    python restamp_score_cache.py --apply
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import argparse
import datetime
import json
import os
import re
import shutil

os.environ.setdefault("NOTION_API_KEY", "unused")
os.environ.setdefault("NOTION_DATABASE_ID", "unused")

from scoring.score_jobs import CACHE_PATH, RUBRIC_FINGERPRINT

# The deal-breaker removed on 2026-10-02: "Public company role requiring
# investor relations or public-market experience". Edit this for the next
# narrow rubric change.
EXCLUDE_PATTERN = re.compile(r"investor relations|public-market|public market", re.IGNORECASE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    with open(CACHE_PATH, encoding="utf-8") as f:
        cache = json.load(f)

    current = already = excluded = restamp = 0
    for entry in cache.values():
        fp = entry.get("rubric_fp")
        if fp == RUBRIC_FINGERPRINT:
            current += 1
            continue
        if entry.get("score") is None:
            continue  # a failed score is retried regardless; nothing to keep
        text = f"{entry.get('reasoning') or ''} {entry.get('ambiguity_note') or ''}"
        if EXCLUDE_PATTERN.search(text):
            excluded += 1
            continue
        restamp += 1
        if args.apply:
            entry["restamped_from"] = fp
            entry["rubric_fp"] = RUBRIC_FINGERPRINT

    print(f"{len(cache)} cache entries; current fingerprint {RUBRIC_FINGERPRINT}")
    print(f"  already current:                {current}")
    print(f"  {'re-stamped' if args.apply else 'would be re-stamped'}:  {restamp}")
    print(f"  left stale (mention the rule):  {excluded}  -> re-scored on the next run")

    if not args.apply:
        print("\n(dry run — nothing written. Re-run with --apply to back up and write.)")
        return
    backup = CACHE_PATH.replace(".json", f".backup-{datetime.date.today().isoformat()}.json")
    shutil.copy2(CACHE_PATH, backup)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)
    print(f"\nBacked up to {os.path.basename(backup)}, then wrote the re-stamped cache.")


if __name__ == "__main__":
    main()

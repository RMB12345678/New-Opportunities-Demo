"""
Tracks which job postings have shown up in a previous run, so each run can
flag which postings are genuinely new versus ones that were already there
last time.

This is separate from scoring/score_jobs.py's cache — that cache exists to
avoid *paying* to re-score an unchanged posting. This file exists to control
what you *see*: a posting can be cached (already scored) but still worth
seeing again if you haven't reviewed it yet, so "new" here means "first time
this URL has ever been scraped," recorded permanently once seen.

Storage: output/seen_jobs.json — a dict of {job_key: first_seen_date}.
"""
import os
import json
from datetime import date, datetime, timezone

SEEN_PATH = os.path.join(os.path.dirname(__file__), "..", "output", "seen_jobs.json")


def _job_key(job):
    return job.get("url") or f"{job.get('company')}::{job.get('title')}"


def _load_seen():
    if os.path.exists(SEEN_PATH):
        with open(SEEN_PATH) as f:
            return json.load(f)
    return {}


def _save_seen(seen):
    os.makedirs(os.path.dirname(SEEN_PATH), exist_ok=True)
    with open(SEEN_PATH, "w") as f:
        json.dump(seen, f, indent=2)


def mark_new_postings(jobs):
    """Given this run's scraped jobs, tag each with:
      - is_new: True if this URL has never been seen in any previous run
      - first_seen: the date it was first scraped (today, if new)
    Also removes postings that have since disappeared from the seen list
    is NOT done here — that's a separate 'still open' concern, handled by
    the fact that jobs.json only ever contains what's currently live.

    Updates and saves the seen-jobs file as a side effect.
    """
    seen = _load_seen()
    today = date.today().isoformat()

    for job in jobs:
        key = _job_key(job)
        if key in seen:
            job["is_new"] = False
            job["first_seen"] = seen[key]
        else:
            job["is_new"] = True
            job["first_seen"] = today
            seen[key] = today

    _save_seen(seen)
    return jobs

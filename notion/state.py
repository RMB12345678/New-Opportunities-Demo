"""
Run-to-run state that Notion itself cannot answer.

Deliberately NOT a mirror of Job Postings rows. An earlier design for this
file held a local copy of each row's score, routing and still-open flag so
the sync could diff against it without re-reading Notion. That is the same
shape as the bug invariant 9 exists to prevent: two independent records of
one fact drift, and the local copy is the one that silently wins. The
paginated Job Postings read was already happening every run and already
returns every property on every page, so diffing against Notion's own
state costs nothing extra and cannot drift from itself.

What is left is the genuinely local part — things no Notion property
records:

  rubric_fingerprint  What the scorer's rubric/model/keyword hash was last
                      run. Notion stores a Score but not what produced it,
                      so a rubric change is invisible from Notion's side.
                      Used only to explain a mass rescore before it
                      happens, never to decide an individual write (see
                      sync_jobs_to_notion for why value comparison decides
                      that instead).
  last_run            The date of the previous completed run. This is what
                      "Last Seen" gets stamped with when a posting closes:
                      the run that closes a job is by definition the run
                      that did NOT see it, so today's date would record the
                      opposite of what the field means.

A missing or unreadable file is normal, not an error — first run ever,
someone cleared output/, a half-written file from a killed run. Every
caller has to work without it, so load() returns defaults rather than
raising.
"""
import json
import os
from datetime import date

STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "output", "notion_state.json")

STATE_VERSION = 1


def load(path=STATE_PATH):
    """Return the saved state, or defaults if there isn't a usable one.

    Swallows a corrupt file on purpose. The cost of ignoring bad state is
    one run that stamps a slightly-wrong Last Seen on closing rows; the
    cost of raising is a pipeline that will not start until someone
    hand-edits a JSON file in output/.
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return _defaults()

    if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
        # A future or unrecognised version: treat as absent rather than
        # guessing at a schema we don't know.
        return _defaults()
    return {
        "version": STATE_VERSION,
        "rubric_fingerprint": data.get("rubric_fingerprint"),
        "last_run": data.get("last_run"),
        "last_run_counts": data.get("last_run_counts") or {},
    }


def _defaults():
    return {"version": STATE_VERSION, "rubric_fingerprint": None,
            "last_run": None, "last_run_counts": {}}


def save(rubric_fingerprint, counts=None, path=STATE_PATH, dry_run=False):
    """Record this run's fingerprint and date.

    Skipped entirely under dry_run, for the same reason mark_new_postings()
    skips writing seen_jobs.json: a rehearsal that persists state consumes
    the very signal it was supposed to be previewing. Advance last_run in a
    dry run and the next real run stamps closing jobs with the date of a
    run that never happened.
    """
    if dry_run:
        print("     [dry-run] would update output/notion_state.json "
              f"(fingerprint {rubric_fingerprint}, last_run {date.today().isoformat()})")
        return None

    payload = {
        "version": STATE_VERSION,
        "rubric_fingerprint": rubric_fingerprint,
        "last_run": date.today().isoformat(),
        "last_run_counts": counts or {},
    }
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    return payload

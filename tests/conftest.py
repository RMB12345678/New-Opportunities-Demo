"""Test setup.

`notion/client.py` reads NOTION_API_KEY and NOTION_DATABASE_ID at import
time, so importing it without a .env raises KeyError before a single test
runs. Tests stub every function that would touch the network, so the values
themselves are never used — they just have to exist. Setting them here keeps
the suite runnable on a machine with no .env at all (a fresh checkout, CI),
which is the whole point of having tests that don't cost money.

ANTHROPIC_API_KEY belongs here for the same reason but fails differently,
which is worth knowing before moving it. `ats_finder/find_ats.py` reads it
into a module-level constant at import time and `find_via_search()` raises
if it is falsy. Setting it inside the test module that needs it is too
late: `ats_finder/find_website.py` imports `find_ats`, so any earlier test
file pulls it in first and caches None, and the tests then fail with
"ANTHROPIC_API_KEY not set" — including, silently, the ones that assert a
RuntimeError is raised, which pass for entirely the wrong reason. conftest
runs before every test module, so it is the only place that fixes the
ordering rather than racing it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("NOTION_API_KEY", "test-key-not-real")
os.environ.setdefault("NOTION_DATABASE_ID", "test-target-list-db")
os.environ.setdefault("JOB_POSTINGS_DATABASE_ID", "test-job-postings-db")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-real")

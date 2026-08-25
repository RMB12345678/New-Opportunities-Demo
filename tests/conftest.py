"""Test setup.

`notion/client.py` reads NOTION_API_KEY and NOTION_DATABASE_ID at import
time, so importing it without a .env raises KeyError before a single test
runs. Tests stub every function that would touch the network, so the values
themselves are never used — they just have to exist. Setting them here keeps
the suite runnable on a machine with no .env at all (a fresh checkout, CI),
which is the whole point of having tests that don't cost money.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("NOTION_API_KEY", "test-key-not-real")
os.environ.setdefault("NOTION_DATABASE_ID", "test-target-list-db")
os.environ.setdefault("JOB_POSTINGS_DATABASE_ID", "test-job-postings-db")

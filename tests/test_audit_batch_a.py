"""Offline tests for audit batch A (punch list #29, #31, #33, #36, #41, #47, #48).

Each one is a data-loss or wasted-money failure found by reading the code at
commit 48d7891. Everything is stubbed: no network, no Notion, no Anthropic,
and nothing touches the real output/ directory.

Runs under pytest.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import notion.client as nc
import run
from scrapers import greenhouse, ashby, workable, pinpoint, teamtailor
from scrapers.common import ScrapeShapeError, require_list
from scoring import score_jobs, track_new_postings

import test_delta_sync as ds


# --- #29: a failed embedded board must not fall through to HTML ------------

def test_failed_embedded_board_is_failed_not_ok(monkeypatch):
    """Neko Health case: detection names an Ashby board, the Ashby scraper
    raises. Falling through to html_generic returned [] on the redirect page,
    stamped the company OK and let the close pass close every row."""
    row = {"company": "Neko Health", "ats_platform": "HTML", "scrape_method": "HTML",
           "careers_url": "https://nekohealth.com/careers", "tier": None}

    monkeypatch.setattr(run, "detect_embedded_board",
                        lambda url, patterns: ("Ashby", "neko", "a redirect", "<html></html>"))

    def boom(company, slug=None):
        raise RuntimeError("Ashby outage")

    monkeypatch.setitem(run.SCRAPERS, "Ashby", boom)

    def html_must_not_run(*a, **k):
        raise AssertionError("fell through to html_generic")

    monkeypatch.setattr(run.html_generic, "fetch_jobs", html_must_not_run)

    jobs, scraped_ok, statuses = run.run_scrapers([row])
    assert jobs == []
    assert "Neko Health" not in scraped_ok
    assert statuses[0]["status"] == run.SCRAPE_FAILED
    assert "Ashby" in statuses[0]["note"]


# --- #31: cache writes are atomic ------------------------------------------

def test_failed_cache_write_leaves_the_old_file_intact(tmp_path, monkeypatch):
    path = tmp_path / "score_cache.json"
    path.write_text(json.dumps({"good": 1}), encoding="utf-8")
    monkeypatch.setattr(score_jobs, "CACHE_PATH", str(path))

    def dump_then_die(obj, f, **kw):
        f.write('{"trunc')
        raise KeyboardInterrupt

    monkeypatch.setattr(score_jobs.json, "dump", dump_then_die)
    with pytest.raises(KeyboardInterrupt):
        score_jobs._save_cache({"new": 2})
    assert json.loads(path.read_text(encoding="utf-8")) == {"good": 1}


def test_cache_and_seen_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(score_jobs, "CACHE_PATH", str(tmp_path / "c.json"))
    monkeypatch.setattr(track_new_postings, "SEEN_PATH", str(tmp_path / "s.json"))
    score_jobs._save_cache({"a": 1})
    track_new_postings._save_seen({"b": 2})
    assert score_jobs._load_cache() == {"a": 1}
    assert track_new_postings._load_seen() == {"b": 2}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["c.json", "s.json"]  # no .tmp left


# --- #33: a failed score keeps Routing, Reasoning and IC Role --------------

def test_real_failure_shape_writes_nothing():
    """The scorer's actual failure result: score None but routing 'Needs
    review', a 'Scoring failed' reasoning and ic_role_flag False."""
    failure = score_jobs._failure_result("rate limited")
    _, writes, _ = ds.sync(
        [ds.jrow(score=8, routing="Scored", reasoning="Good fit", ic_role=True)],
        [ds.job(**failure)])
    assert writes == [], writes


# --- #36: a missing key is not an empty board ------------------------------

class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class _Session:
    def __init__(self, payload):
        self._p = payload

    def get(self, *a, **k):
        return _Resp(self._p)


SCRAPERS_BY_KEY = [(greenhouse, "jobs"), (ashby, "jobs"), (workable, "jobs"),
                   (pinpoint, "data"), (teamtailor, "items")]


@pytest.mark.parametrize("module,key", SCRAPERS_BY_KEY)
def test_missing_key_raises_and_empty_list_is_a_real_empty_board(module, key, monkeypatch):
    monkeypatch.setattr(module, "session", _Session({"error": "maintenance"}))
    with pytest.raises(ScrapeShapeError):
        module.fetch_jobs("Acme", slug="acme")
    monkeypatch.setattr(module, "session", _Session({key: []}))
    assert module.fetch_jobs("Acme", slug="acme") == []


def test_require_list_rejects_non_dict_and_non_list():
    for bad in ([], None, {"jobs": None}, {"jobs": {}}):
        with pytest.raises(ScrapeShapeError):
            require_list(bad, "jobs", "X")


# --- #41: the rubric is read as UTF-8 --------------------------------------

def test_rubric_is_decoded_as_utf8():
    """RUBRIC_TEXT must equal the file read as UTF-8, whatever the platform's
    default encoding is. (Independent of the rubric's own wording, so it holds
    for any rubric, including the public demo's.)"""
    with open(score_jobs._RUBRIC_PATH, encoding="utf-8") as f:
        assert score_jobs.RUBRIC_TEXT == f.read()
    assert "â€" not in score_jobs.RUBRIC_TEXT  # cp1252 mojibake of an em-dash


# --- #47: no hardcoded Job Postings database id ----------------------------

def test_job_postings_id_has_no_fallback():
    src = open(os.path.join(os.path.dirname(nc.__file__), "client.py"), encoding="utf-8").read()
    assert '_require_env("JOB_POSTINGS_DATABASE_ID")' in src
    assert 'environ.get("JOB_POSTINGS_DATABASE_ID"' not in src
    import re
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", src),         "no hardcoded database id may come back"


# --- #48: blank-named Companies rows are skipped ---------------------------

def _co(name, **kw):
    base = {"page_id": "p-" + str(name), "company": name, "sector": None, "hq": None,
            "source": None, "ats_platform": None, "scrape_method": None,
            "careers_url": None, "website": None, "has_icon": False, "tier": None}
    base.update(kw)
    return base


def test_selectors_skip_unnamed_rows(monkeypatch, capsys):
    rows = [_co(None), _co("  "), _co("Acme")]
    monkeypatch.setattr(nc, "get_all_companies", lambda: rows)
    assert [r["company"] for r in nc.get_companies_missing_ats()] == ["Acme"]
    assert [r["company"] for r in nc.get_companies_missing_website()] == ["Acme"]
    assert "skipped 2 Companies row(s)" in capsys.readouterr().out

    rows[:] = [_co(None, ats_platform="Greenhouse"), _co("Acme", ats_platform="Greenhouse")]
    assert [r["company"] for r in nc.get_companies_missing_profile()] == ["Acme"]
    assert [r["company"] for r in nc.get_companies_missing_url()] == ["Acme"]


def test_run_scrapers_skips_unnamed_rows(capsys):
    row = _co(None, ats_platform="Greenhouse")
    jobs, scraped_ok, statuses = run.run_scrapers([row])
    assert jobs == [] and statuses == [] and not scraped_ok
    assert "skipped 1 Companies row(s)" in capsys.readouterr().out

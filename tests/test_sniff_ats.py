"""Tests for sniff_ats_from_page(): reading a branded careers page's source
for the real ATS. Pages are passed in as html, so nothing touches the network.

The property worth protecting is the same one as invariant 19: a page naming
more than one board (or platform) must answer None, never "the first one".
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ats_finder.find_ats import sniff_ats_from_page, ats_is_unresolved

URL = "https://careers.example.com"


def test_finds_a_workday_board_and_drops_locale_and_job_path():
    html = '<a href="https://acme.wd5.myworkdayjobs.com/en-US/Acme_Careers/job/NY/VP_R1">'
    r = sniff_ats_from_page(URL, html=html)
    assert r["platform"] == "Workday"
    assert r["board_url"] == "https://acme.wd5.myworkdayjobs.com/Acme_Careers"


def test_json_escaped_urls_are_read():
    html = r'{"u":"https:\/\/acme.wd1.myworkdayjobs.com\/Ext"}'
    assert sniff_ats_from_page(URL, html=html)["board_url"] == \
        "https://acme.wd1.myworkdayjobs.com/Ext"


def test_oracle_recruiting_cloud():
    html = '<iframe src="https://eabc.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001">'
    r = sniff_ats_from_page(URL, html=html)
    assert r["platform"] == "Oracle Recruiting Cloud"
    assert r["board_url"].endswith("/sites/CX_1001")


def test_icims_ignores_static_asset_hosts():
    assert sniff_ats_from_page(URL, html='<script src="https://static.icims.com/a.js">') is None
    r = sniff_ats_from_page(URL, html='<a href="https://careers-acme.icims.com/jobs/search">')
    assert r["platform"] == "iCIMS"


def test_two_boards_of_one_platform_is_no_answer():
    html = ("https://a.wd1.myworkdayjobs.com/A https://b.wd1.myworkdayjobs.com/B")
    assert sniff_ats_from_page(URL, html=html) is None


def test_two_platforms_is_no_answer():
    html = "https://a.wd1.myworkdayjobs.com/A https://careers-a.icims.com/jobs"
    assert sniff_ats_from_page(URL, html=html) is None


def test_taleo_and_plain_pages_are_no_answer():
    assert sniff_ats_from_page(URL, html="https://x.taleo.net/careersection") is None
    assert sniff_ats_from_page(URL, html="<html>no ats here</html>") is None


def test_unresolved_answers():
    assert ats_is_unresolved("None (in-house careers page)")
    assert ats_is_unresolved("Not found")
    assert not ats_is_unresolved("Workday")
    assert not ats_is_unresolved("Phenom People")

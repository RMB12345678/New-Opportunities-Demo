"""Tests for how find_via_search() reads a research reply (punch list #3).

The bug these protect against is the most expensive shape in the codebase:
a reply that comes back HTTP 200 but never finished. `max_tokens` was 500
with web search enabled, so a model that narrated its searching before
answering ran out of room mid-block. Nothing checked `stop_reason`, every
`_extract()` fell through to its default, and "Not found (auto-search
failed)" went into Notion as a *permanent* marker.

Permanent is what makes it critical rather than annoying. Invariant 8 makes
a non-blank value mean "already researched", so the company is never
re-researched, and `get_companies_missing_url()` separately excludes any
row whose ATS contains "Not found", so it loses the careers-URL backfill
too. The company leaves the pipeline on the strength of a reply that never
finished. 36 rows were in that state when this was written.

Invariant 1 is the rule being restored: a search that failed to RUN is not
a search that ran and found nothing. Raising puts the company back in the
retry path; returning a default condemns it.

Every HTTP call is stubbed, so the suite makes no network calls, spends
nothing, and needs no credentials.

Runs under pytest, and standalone (`python tests/test_research_reply.py`).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ats_finder import find_ats

# conftest sets ANTHROPIC_API_KEY before any test module imports find_ats,
# which is what makes the pytest path work — see its docstring for why
# setting it in this module instead would be too late. conftest does not run
# on the standalone path (`python tests/test_research_reply.py`), and
# find_via_search() raises if the key is falsy, so the constant is topped up
# directly here too. `or` rather than a plain assignment: on a machine with a
# real .env the real key is already loaded, and there is no reason to stomp
# on it even though nothing in this file reaches the network.
find_ats.ANTHROPIC_API_KEY = find_ats.ANTHROPIC_API_KEY or "test-key-not-real"

# A well-formed reply, as the prompt asks for it.
GOOD_REPLY = """AMBIGUOUS: false
AMBIGUITY_NOTE:
SECTOR: Surgical robotics for soft-tissue procedures
HQ: Cambridge, UK
ATS: Greenhouse
METHOD: API
URL: https://boards.greenhouse.io/acmesurgical
HOMEPAGE: https://acmesurgical.com"""


class FakeResponse:
    def __init__(self, text_blocks, stop_reason="end_turn", status=200):
        self.ok = status < 400
        self.status_code = status
        self._payload = {
            "stop_reason": stop_reason,
            "content": [{"type": "text", "text": t} for t in text_blocks],
        }
        self.text = ""

    def json(self):
        return self._payload


def stub(response):
    """Point find_ats's shared session at a canned reply."""
    class FakeSession:
        def post(self, url, **kwargs):
            stub.last_payload = kwargs.get("json")
            return response
    find_ats.session = FakeSession()


def restore():
    from http_client import session as real
    find_ats.session = real


def search(reply_blocks, stop_reason="end_turn"):
    stub(FakeResponse(reply_blocks, stop_reason=stop_reason))
    try:
        return find_ats.find_via_search("Acme Surgical")
    finally:
        restore()


# --- step 1: a truncated reply must raise ----------------------------------

def test_truncated_reply_raises_instead_of_writing_not_found():
    """The whole bug in one test. stop_reason "max_tokens" means the model
    ran out of room; the partial text must never be parsed into an answer,
    because that answer becomes a permanent marker in Notion."""
    try:
        search(["AMBIGUOUS: false\nSECTOR: Surgical rob"], stop_reason="max_tokens")
    except RuntimeError as e:
        assert "token cap" in str(e), "the error must name the real cause"
        assert "Acme Surgical" in str(e), "and the company, since it lands in a printed note"
    else:
        raise AssertionError("a truncated reply must raise, not return a default")


def test_truncated_reply_raises_even_when_the_text_looks_complete():
    """A reply can be cut off after the last line it managed to emit and
    still look parseable. stop_reason is the only reliable signal, so it is
    checked before the text is looked at at all."""
    try:
        search([GOOD_REPLY], stop_reason="max_tokens")
    except RuntimeError:
        pass
    else:
        raise AssertionError("stop_reason must be checked before parsing, not after")


def test_a_finished_reply_is_parsed_normally():
    info = search([GOOD_REPLY])
    assert info["ats_platform"] == "Greenhouse"
    assert info["sector"] == "Surgical robotics for soft-tissue procedures"
    assert info["hq"] == "Cambridge, UK"
    assert info["careers_url"] == "https://boards.greenhouse.io/acmesurgical"
    assert info["ambiguous"] is False


# --- step 2: the token cap that caused it ----------------------------------

def test_the_token_cap_is_high_enough_for_search_narration():
    """500 was not enough room for web-search narration plus the block,
    which is what made truncation routine rather than rare."""
    search([GOOD_REPLY])
    assert stub.last_payload["max_tokens"] >= 1500


# --- step 3: a reply that ignored the format -------------------------------

def test_a_reply_with_no_structured_fields_raises():
    """A refusal or a pure-prose answer parses to nothing. Defaulting would
    condemn the company on a reply that was never in the right shape."""
    try:
        search(["I could not find any information about this company."])
    except RuntimeError as e:
        assert "none of the" in str(e)
    else:
        raise AssertionError("a reply with no structured fields must raise")


def test_a_partial_but_real_answer_is_still_an_answer():
    """Only ALL THREE being absent means the format was ignored. A reply
    that answered some fields and left others blank genuinely ran, and its
    blanks are real answers — raising on those would re-research companies
    that were already honestly researched, which is the waste invariant 8
    exists to prevent."""
    info = search(["AMBIGUOUS: false\nSECTOR: Medical devices\nATS:\nMETHOD: Manual check"])
    assert info["sector"] == "Medical devices"
    assert info["ats_platform"] == "", "a present-but-empty ATS line is an answer, not a failure"


# --- step 4: the extractor -------------------------------------------------

def test_prose_before_the_block_does_not_win():
    """With web search on the model narrates first, and that narration can
    contain something that looks like a labelled line. The real block is
    the one it ends with."""
    reply = ("Let me search. Their careers page mentions ATS: something in my notes.\n"
             "Here is the summary:\n" + GOOD_REPLY)
    info = search([reply])
    assert info["ats_platform"] == "Greenhouse", \
        "the last ATS line is the answer; the first was the model thinking aloud"
    assert info["hq"] == "Cambridge, UK"


def test_a_label_must_start_its_line():
    """Unanchored matching let a label buried mid-sentence win outright."""
    reply = "The company's STATS: misleading\n" + GOOD_REPLY
    info = search([reply])
    assert info["ats_platform"] == "Greenhouse"


def test_labels_are_not_confused_with_longer_labels():
    """AMBIGUOUS and AMBIGUITY_NOTE share a prefix; the colon is what
    separates them, so neither may capture the other's value."""
    reply = ("AMBIGUOUS: true\n"
             "AMBIGUITY_NOTE: Two companies share this name.\n"
             "SECTOR: AMBIGUOUS — see note\n"
             "HQ: AMBIGUOUS — see note\n"
             "ATS: AMBIGUOUS — see note\n"
             "METHOD: Manual check\nURL: N/A\nHOMEPAGE: N/A")
    info = search([reply])
    assert info["ambiguous"] is True
    assert info["ambiguity_note"] == "Two companies share this name."
    assert info["careers_url"] is None


# --- the contract these all serve ------------------------------------------

def test_every_failure_mode_raises_rather_than_returning_a_marker():
    """Invariant 1, stated as a test. None of these may produce a dict: a
    returned dict becomes a permanent Notion value, and every one of these
    inputs means "we did not get an answer", not "there is no answer"."""
    cases = [
        (["AMBIGUOUS: false\nSECT"], "max_tokens"),
        (["Sorry, I cannot help with that."], "end_turn"),
        ([""], "end_turn"),
    ]
    for blocks, stop in cases:
        try:
            search(blocks, stop_reason=stop)
        except RuntimeError:
            continue
        raise AssertionError(f"{blocks!r} (stop_reason={stop}) should have raised")


if __name__ == "__main__":
    # Standalone runner, for when pytest isn't installed.
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print("PASS  %s" % name)
        except AssertionError as e:
            failures += 1
            print("FAIL  %s: %s" % (name, e))
    print("\n%d passed, %d failed" % (
        len([n for n in globals() if n.startswith("test_")]) - failures, failures))
    sys.exit(1 if failures else 0)

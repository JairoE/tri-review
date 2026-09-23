"""The ledger of findings a human already rejected."""

from __future__ import annotations

from pathlib import Path

import pytest

from tri_review import dismissals


def test_no_ledger_is_not_an_error():
    """Most repos never dismiss anything; that is the normal case.

    None is also what every failed lookup produces -- see graph._trusted_ledger
    -- so this is the path taken whenever a ledger cannot be trusted, not only
    when none exists.
    """
    assert dismissals.parse(None) == []


def test_loads_claim_reason_and_location():
    entries = dismissals.parse("""
[[dismissed]]
file = "src/a.py"
claim = "leaks a handle"
reason = "checked, the context manager closes it"
date = "2026-01-01"
""")
    (entry,) = entries
    assert entry.claim == "leaks a handle"
    assert entry.reason == "checked, the context manager closes it"
    assert entry.file == "src/a.py"


def test_an_entry_without_a_reason_is_rejected():
    """A dismissal with no reason is suppression.

    The reason is what the report shows the reader so they can see why a
    finding was set aside. Without one there is nothing to show, and the entry
    would silently bury a finding.
    """
    with pytest.raises(dismissals.MalformedDismissals, match="no 'reason'"):
        dismissals.parse('[[dismissed]]\nclaim = "x"\n')


def test_an_entry_without_a_claim_is_rejected():
    with pytest.raises(dismissals.MalformedDismissals, match="no 'claim'"):
        dismissals.parse('[[dismissed]]\nreason = "because"\n')


def test_malformed_toml_fails_loudly():
    """A typo must not quietly drop every dismissal.

    Swallowing this would let settled findings return with no indication that
    the ledger had stopped working -- the exact failure the ledger prevents.
    """
    with pytest.raises(dismissals.MalformedDismissals):
        dismissals.parse("[[dismissed]\nclaim = ")


def test_blank_file_is_no_dismissals():
    assert dismissals.parse("   \n") == []


def test_render_is_empty_when_there_is_nothing_to_say():
    """No dismissals must add no message, so the synthesizer prompt is unchanged."""
    assert dismissals.render([]) == ""


def test_render_tells_the_synthesizer_not_to_suppress():
    """Downgrade, never hide -- a wrong match has to stay visible to the reader."""
    text = dismissals.render([dismissals.Dismissal(claim="c", reason="r")])
    assert "do not promote it to Blocking" in text
    assert "previously rejected" in text


def test_this_repo_s_own_ledger_parses():
    """The checked-in .tri-review/dismissed.toml must actually load.

    It may legitimately be empty -- a dismissal whose claim no longer describes
    the code should be removed, not kept -- but it must never be malformed,
    because a malformed ledger read at the base ref fails every PR's review.
    """
    text = (Path(__file__).resolve().parent.parent / dismissals.DISMISSALS_PATH).read_text()
    entries = dismissals.parse(text)
    assert all(e.claim and e.reason for e in entries)

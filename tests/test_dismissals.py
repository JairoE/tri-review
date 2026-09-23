"""The ledger of findings a human already rejected."""

from __future__ import annotations

from pathlib import Path

import pytest

from tri_review import dismissals


def _write(tmp_path, body: str) -> Path:
    (tmp_path / ".tri-review").mkdir()
    (tmp_path / dismissals.DISMISSALS_PATH).write_text(body)
    return tmp_path


def test_no_file_is_not_an_error(tmp_path):
    """Most repos never dismiss anything; that is the normal case."""
    assert dismissals.read_local(tmp_path) is None
    assert dismissals.parse(None) == []


def test_loads_claim_reason_and_location(tmp_path):
    root = _write(tmp_path, """
[[dismissed]]
file = "src/a.py"
claim = "leaks a handle"
reason = "checked, the context manager closes it"
date = "2026-01-01"
""")
    (entry,) = dismissals.parse(dismissals.read_local(root))
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


def test_an_unreadable_ledger_is_not_treated_as_an_absent_one(tmp_path):
    """Only a genuinely missing file means "no dismissals".

    A directory sitting at the ledger's path is a ledger that exists and could
    not be read. Reporting that as "none recorded" would silently drop every
    entry -- so it raises instead.
    """
    (tmp_path / ".tri-review").mkdir()
    (tmp_path / dismissals.DISMISSALS_PATH).mkdir()
    with pytest.raises(dismissals.MalformedDismissals):
        dismissals.read_local(tmp_path)


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
    """The checked-in .tri-review/dismissed.toml must actually load."""
    entries = dismissals.parse(dismissals.read_local(Path(__file__).resolve().parent.parent))
    assert entries and all(e.claim and e.reason for e in entries)

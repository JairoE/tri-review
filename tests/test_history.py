"""Stored runs must replay only when they still answer the same question."""

from tri_review import history
from tri_review.schema import Finding, ReviewResult


def _record(**overrides):
    base = dict(
        repo="octocat/Hello-World",
        pr="42",
        head_sha="abc123def456",
        models=["gpt-5.6-terra", "claude-sonnet-5"],
        excludes=["**/*.md"],
        report="## Consensus Findings\n\nNone.",
        results=[
            ReviewResult(
                model="gpt-5.6-terra",
                findings=[
                    Finding(
                        file="auth.py",
                        line=7,
                        severity="critical",
                        category="security",
                        title="MD5 password hash",
                        detail="Fast and unsalted.",
                    )
                ],
            )
        ],
    )
    base.update(overrides)
    return history.RunRecord(**base)


def test_findings_survive_the_roundtrip(tmp_path):
    """Structured findings, not just the prose, are what a later diff compares."""
    history.save(_record(), tmp_path)
    loaded = history.load("octocat/Hello-World", "42", tmp_path)
    assert loaded is not None
    assert loaded.results[0].findings[0].title == "MD5 password hash"
    assert loaded.results[0].findings[0].line == 7
    assert loaded.head_sha == "abc123def456"


def test_missing_history_is_a_miss_not_an_error(tmp_path):
    assert history.load("octocat/Hello-World", "1", tmp_path) is None


def test_corrupt_history_is_a_miss_not_a_crash(tmp_path):
    path = history.path_for("octocat/Hello-World", "42", tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json at all", encoding="utf-8")
    assert history.load("octocat/Hello-World", "42", tmp_path) is None


def test_a_future_schema_is_a_miss(tmp_path):
    path = history.path_for("octocat/Hello-World", "42", tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"schema": 999, "repo": "x/y"}', encoding="utf-8")
    assert history.load("octocat/Hello-World", "42", tmp_path) is None


def test_repo_name_cannot_escape_the_history_directory(tmp_path):
    path = history.path_for("../../etc/passwd", "1", tmp_path)
    assert path.parent == tmp_path
    assert ".." not in path.name


def test_an_unwritable_directory_does_not_sink_a_paid_run(tmp_path):
    blocker = tmp_path / "cache"
    blocker.write_text("I am a file, not a directory", encoding="utf-8")
    assert history.save(_record(), blocker) is None


def test_replays_when_nothing_changed():
    record = _record()
    assert (
        history.stale_reason(
            record, "abc123def456", ["gpt-5.6-terra", "claude-sonnet-5"], ("**/*.md",)
        )
        is None
    )


def test_a_new_head_sha_is_different_code():
    reason = history.stale_reason(
        _record(), "999fff", ["gpt-5.6-terra", "claude-sonnet-5"], ("**/*.md",)
    )
    assert reason is not None and "moved on" in reason


def test_a_changed_panel_answers_a_different_question():
    """Replaying Sonnet's review when Opus was asked for would be a wrong answer."""
    reason = history.stale_reason(
        _record(), "abc123def456", ["gpt-5.6-terra", "claude-opus-5"], ("**/*.md",)
    )
    assert reason is not None and "panel changed" in reason


def test_changed_excludes_mean_a_different_slice_of_the_pr():
    reason = history.stale_reason(
        _record(), "abc123def456", ["gpt-5.6-terra", "claude-sonnet-5"], ()
    )
    assert reason is not None and "exclude" in reason


def test_an_unknown_head_sha_never_replays():
    """Without a SHA to compare, "unchanged" cannot be established."""
    assert history.stale_reason(_record(), "", ["gpt-5.6-terra", "claude-sonnet-5"], ("**/*.md",))


def test_an_empty_stored_report_is_not_worth_replaying():
    reason = history.stale_reason(
        _record(report="  "), "abc123def456", ["gpt-5.6-terra", "claude-sonnet-5"], ("**/*.md",)
    )
    assert reason is not None and "no report" in reason

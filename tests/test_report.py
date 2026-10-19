"""Normalizing synthesized findings: R4 tag enforcement, R5 IDs and order."""

from tri_review.report import normalize
from tri_review.schema import ReportFinding, SynthesisOutput

PANEL = ["gpt-6-sol", "claude-sonnet-5", "gemini-3-pro"]


def make(**overrides) -> ReportFinding:
    base = dict(
        reported_by=["gpt-6-sol", "claude-sonnet-5"],
        severity="major",
        category="bug",
        file="src/auth.py",
        line=42,
        title="Token refresh races logout",
        problem="refresh() writes the token after logout() cleared it.",
        trigger="Logout while a refresh request is in flight.",
        fix="Check the session is still set before assigning.",
        action="blocking",
        falsified_by="A lock around refresh and logout.",
    )
    base.update(overrides)
    return ReportFinding(**base)


def only(findings: list[ReportFinding], reported=PANEL):
    [result] = normalize(findings, reported)
    return result


def test_unknown_reporters_are_dropped():
    result = only(
        [make(reported_by=["gpt-6-sol", "made-up-model", "gpt-6-sol", "claude-sonnet-5"])]
    )
    assert result.reported_by == ["gpt-6-sol", "claude-sonnet-5"]


def test_unknown_reporters_do_not_count_toward_consensus():
    result = only([make(reported_by=["gpt-6-sol", "made-up-model"])])
    assert result.kind == "unique"
    assert result.action == "optional"


def test_two_reporters_is_consensus_one_is_unique_zero_is_synthesizer():
    consensus, unique, synthesizer = (
        only([make(reported_by=by, action="optional")])
        for by in (["gpt-6-sol", "claude-sonnet-5"], ["gemini-3-pro"], [])
    )
    assert consensus.kind == "consensus"
    assert unique.kind == "unique"
    assert synthesizer.kind == "synthesizer"


def test_reporters_that_failed_this_run_are_dropped():
    result = only([make()], reported=["gpt-6-sol"])
    assert result.reported_by == ["gpt-6-sol"]
    assert result.kind == "unique"


def test_valid_blocking_is_kept():
    result = only([make()])
    assert result.action == "blocking"
    assert result.downgrade_reason is None


def test_blocking_single_model_is_downgraded_with_reason():
    result = only([make(reported_by=["gpt-6-sol"])])
    assert result.action == "optional"
    assert result.downgrade_reason == "only one model reported this"


def test_blocking_minor_is_downgraded():
    result = only([make(severity="minor")])
    assert result.action == "optional"
    assert result.downgrade_reason == "severity is minor"


def test_blocking_without_falsifier_is_downgraded():
    for falsified_by in (None, "", "   "):
        result = only([make(falsified_by=falsified_by)])
        assert result.action == "optional"
        assert result.downgrade_reason == "no falsifier named"


def test_valid_pending_check_is_kept():
    result = only([make(action="blocking_pending_check", check="GET /session after logout")])
    assert result.action == "blocking_pending_check"
    assert result.downgrade_reason is None


def test_pending_check_without_check_is_downgraded():
    result = only([make(action="blocking_pending_check", check=None)])
    assert result.action == "optional"
    assert result.downgrade_reason == "no check named"


def test_pending_check_needs_consensus_severity_and_falsifier_too():
    cases = {
        "only one model reported this": dict(reported_by=["gpt-6-sol"]),
        "severity is minor": dict(severity="minor"),
        "no falsifier named": dict(falsified_by=None),
    }
    for reason, overrides in cases.items():
        result = only([make(action="blocking_pending_check", check="run it", **overrides)])
        assert result.action == "optional"
        assert result.downgrade_reason == reason


def test_previously_rejected_without_new_evidence_is_optional():
    result = only([make(previously_rejected="Logout holds the lock already.")])
    assert result.action == "optional"
    assert result.downgrade_reason == "previously rejected, no new evidence"
    assert result.previously_rejected == "Logout holds the lock already."


def test_previously_rejected_with_new_evidence_keeps_its_tag():
    result = only(
        [
            make(
                previously_rejected="Logout holds the lock already.",
                new_evidence="This diff removes the lock from logout().",
            )
        ]
    )
    assert result.action == "blocking"
    assert result.downgrade_reason is None


def test_synthesizer_suggestion_is_always_optional():
    for action in ("blocking", "blocking_pending_check"):
        result = only([make(reported_by=[], action=action, check="run it")])
        assert result.kind == "synthesizer"
        assert result.action == "optional"
        assert result.downgrade_reason == "suggested by the synthesizer, not reported by any reviewer"


def test_downgrade_reason_precedence_when_several_rules_apply():
    rejected = "Logout holds the lock already."
    cases = [
        (dict(reported_by=[], previously_rejected=rejected), "suggested by the synthesizer"),
        (dict(reported_by=["gpt-6-sol"], previously_rejected=rejected), "previously rejected"),
        (
            dict(reported_by=["gpt-6-sol"], previously_rejected=rejected, new_evidence="New."),
            "only one model",
        ),
        (dict(severity="minor", falsified_by=None), "severity is minor"),
    ]
    for overrides, reason in cases:
        result = only([make(**overrides)])
        assert result.action == "optional"
        assert result.downgrade_reason.startswith(reason), overrides


def test_optional_is_never_given_a_downgrade_reason():
    result = only([make(reported_by=[], action="optional")])
    assert result.action == "optional"
    assert result.downgrade_reason is None


def test_ids_follow_action_then_severity_then_file_then_line():
    findings = [
        make(title="opt-minor", action="optional", severity="minor"),
        make(title="opt-crit", action="optional", severity="critical"),
        make(title="pending", action="blocking_pending_check", check="run it"),
        make(title="block-b-50", file="b.py", line=50),
        make(title="block-b-7", file="b.py", line=7),
        make(title="block-a", file="a.py", line=99),
        make(title="block-crit", severity="critical", file="z.py"),
    ]
    result = normalize(findings, PANEL)
    assert [f.title for f in result] == [
        "block-crit",
        "block-a",
        "block-b-7",
        "block-b-50",
        "pending",
        "opt-crit",
        "opt-minor",
    ]
    assert [f.id for f in result] == [f"F{n}" for n in range(1, 8)]


def test_order_uses_the_action_after_downgrades():
    findings = [
        make(title="optional", action="optional", severity="critical"),
        make(title="downgraded", reported_by=["gpt-6-sol"]),
    ]
    assert [f.title for f in normalize(findings, PANEL)] == ["optional", "downgraded"]


def test_null_lines_sort_last():
    findings = [
        make(title="no-line", line=None),
        make(title="line-900", line=900),
        make(title="line-1", line=1),
    ]
    assert [f.title for f in normalize(findings, PANEL)] == ["line-1", "line-900", "no-line"]


def test_normalized_findings_start_not_applyable():
    result = only([make()])
    assert result.applyable is False
    assert result.inline_reason is None


def test_normalize_does_not_mutate_its_input():
    original = make(reported_by=["gpt-6-sol", "made-up-model"])
    normalize([original], PANEL)
    assert original.reported_by == ["gpt-6-sol", "made-up-model"]
    assert original.action == "blocking"


def test_report_finding_fix_range_follows_r2_rules():
    assert make(fix_start_line=42, replacement="x").fix_end_line == 42
    inverted = make(fix_start_line=44, fix_end_line=42, replacement="x")
    assert (inverted.fix_start_line, inverted.fix_end_line, inverted.replacement) == (None,) * 3


def test_report_finding_without_trigger_is_kept_with_empty_string():
    data = make().model_dump()
    del data["trigger"]
    assert ReportFinding.model_validate(data).trigger == ""
    assert make(trigger="  Logout mid-refresh.  ").trigger == "Logout mid-refresh."


def test_one_malformed_report_finding_costs_only_itself():
    good = make().model_dump()
    bad = {**make(file="src/bad.py").model_dump(), "action": "urgent"}
    output = SynthesisOutput.model_validate({"findings": [good, bad, good]})
    assert len(output.findings) == 2
    assert len(output.malformed) == 1
    assert output.malformed[0].startswith("findings[1] (src/bad.py): action:")


def test_malformed_is_not_in_the_model_facing_schema():
    assert "malformed" not in SynthesisOutput.model_json_schema()["properties"]

"""Normalizing synthesized findings: R4 tag enforcement, R5 IDs and order."""

from tri_review import report
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


# --- Rendering: R6 blocks, report layout, fences (Task 5) -------------------

REPO = "o/r"
SHA = "abc1234def5678"
FIX_CODE = "if self._session is not None:\n    self._session.token = new_token"


def norm(**overrides):
    """One finding through normalize(); on PANEL, two reporters means Consensus."""
    return only([make(**overrides)])


def block(finding, repo=REPO, sha=SHA, **kwargs) -> str:
    return report.render_block(finding, repo, sha, **kwargs)


def test_block_has_problem_trigger_fix_lines():
    finding = norm(fix_start_line=42, fix_end_line=44, replacement=FIX_CODE, file="src/auth.py")
    lines = block(finding).splitlines()
    problem = "**Problem:** refresh() writes the token after logout() cleared it."
    trigger = "**Trigger:** Logout while a refresh request is in flight."
    fix = "**Fix:** Check the session is still set before assigning."
    assert lines[0] == "### F1 · **major** · bug — Token refresh races logout"
    assert lines[lines.index(problem) + 1] == trigger
    assert lines[lines.index(trigger) + 1] == fix
    assert "```python" in lines
    assert FIX_CODE.splitlines()[0] in lines


def test_empty_trigger_renders_not_stated():
    text = block(norm(trigger=""))
    assert "**Trigger:** _not stated_" in text


def test_missing_fix_is_omitted():
    text = block(norm(fix=None))
    assert "**Fix:**" not in text
    assert "**Problem:**" in text


def test_fix_range_wins_over_line_in_location():
    finding = norm(line=40, fix_start_line=42, fix_end_line=44, replacement="x")
    text = block(finding)
    assert "`src/auth.py:42-44`" in text
    assert "src/auth.py:40" not in text
    single = block(norm(line=40, fix_start_line=42, fix_end_line=42, replacement="x"))
    assert "`src/auth.py:42`" in single


def test_location_links_to_head_when_repo_and_sha_known():
    ranged = block(norm(fix_start_line=42, fix_end_line=44, replacement="x"))
    assert f"[`src/auth.py:42-44`](https://github.com/o/r/blob/{SHA}/src/auth.py#L42-L44)" in ranged
    single = block(norm(line=42))
    assert f"[`src/auth.py:42`](https://github.com/o/r/blob/{SHA}/src/auth.py#L42)" in single
    bare = block(norm(line=None))
    assert f"[`src/auth.py`](https://github.com/o/r/blob/{SHA}/src/auth.py)" in bare


def test_location_is_plain_without_repo():
    for repo, sha in (("", SHA), (REPO, ""), ("", "")):
        text = block(norm(line=42), repo, sha)
        assert "`src/auth.py:42`" in text
        assert "](" not in text and "github.com" not in text


def test_consensus_block_names_every_reporter():
    text = block(norm())
    assert "`gpt-6-sol`, `claude-sonnet-5`" in text
    assert "unverified" not in text


def test_unique_block_names_model_as_unverified():
    text = block(norm(reported_by=["gemini-3-pro"], action="optional"))
    assert "**`gemini-3-pro`** (unverified by the other reviewers)" in text


def test_synthesizer_suggestion_is_labelled():
    text = block(norm(reported_by=[], action="optional"))
    assert "suggested by the synthesizer, not reported by any reviewer" in text


def test_downgrade_reason_is_shown():
    text = block(norm(reported_by=["gpt-6-sol"]))
    assert "_Optional — only one model reported this_" in text
    assert "Optional —" not in block(norm())


def test_previously_rejected_reason_and_new_evidence_shown():
    rejected = norm(previously_rejected="Logout holds the lock already.")
    text = block(rejected)
    assert "Previously rejected: Logout holds the lock already." in text
    assert "New evidence" not in text
    evidence = norm(
        previously_rejected="Logout holds the lock already.",
        new_evidence="This diff removes the lock from logout().",
    )
    text = block(evidence)
    assert "Previously rejected: Logout holds the lock already." in text
    assert "New evidence: This diff removes the lock from logout()." in text


def test_inline_status_line_posted_vs_reason():
    base = dict(fix_start_line=42, fix_end_line=44, replacement="x")
    posted = norm(**base).model_copy(update={"applyable": True})
    assert block(posted).splitlines()[-1] == "_Inline suggestion posted on L42-44._"
    single = norm(fix_start_line=42, fix_end_line=42, replacement="x").model_copy(
        update={"applyable": True}
    )
    assert block(single).splitlines()[-1] == "_Inline suggestion posted on L42._"
    refused = norm(**base).model_copy(update={"inline_reason": "lines outside the diff"})
    assert block(refused).splitlines()[-1] == "_No inline suggestion: lines outside the diff._"
    # No replacement: no status line at all, whatever the flags say.
    assert "nline suggestion" not in block(norm(replacement=None))


def test_with_id_false_drops_id_reporters_and_status():
    finding = norm(fix_start_line=42, fix_end_line=44, replacement="x").model_copy(
        update={"applyable": True}
    )
    text = block(finding, with_id=False)
    assert text.splitlines()[0] == "### **major** · bug — Token refresh races logout"
    assert "F1" not in text
    assert "gpt-6-sol" not in text and "synthesizer" not in text
    assert "nline suggestion" not in text


def test_next_steps_one_bullet_per_fix_with_falsifier_or_check():
    findings = [
        make(title="blocker", line=42),
        make(
            title="pending",
            action="blocking_pending_check",
            check="GET /session after logout",
            line=50,
            fix="Re-read the session.",
        ),
        make(title="nice", action="optional", line=60, fix="Rename it.", falsified_by=None),
        make(title="no fix", action="optional", line=70, fix=None),
    ]
    text = report.render_report(normalize(findings, PANEL), REPO, SHA)
    steps = text.split("## Actionable Next Steps\n", 1)[1].strip().splitlines()
    assert steps == [
        "- **Blocking** F1 · `src/auth.py:42` — Check the session is still set before assigning."
        " Falsified by: A lock around refresh and logout.",
        "- **Blocking pending check** F2 · `src/auth.py:50` — Re-read the session."
        " Check: GET /session after logout. Falsified by: A lock around refresh and logout.",
        "- **Optional** F3 · `src/auth.py:60` — Rename it.",
    ]


def test_report_sections_and_membership():
    findings = [
        make(title="agreed"),
        make(title="solo", reported_by=["gemini-3-pro"], action="optional"),
        make(title="invented", reported_by=[], action="optional"),
    ]
    text = report.render_report(normalize(findings, PANEL), REPO, SHA)
    consensus, rest = text.split("## Unique Insights", 1)
    unique, steps = rest.split("## Actionable Next Steps", 1)
    assert consensus.startswith("## Consensus Findings")
    assert "agreed" in consensus and "solo" not in consensus
    assert "solo" in unique and "invented" in unique and "agreed" not in unique


def test_empty_sections_say_so_in_one_line():
    text = report.render_report([], REPO, SHA)
    assert "## Consensus Findings\n\n_No consensus findings._\n" in text
    assert "## Unique Insights\n\n_No unique insights._\n" in text
    assert "## Actionable Next Steps\n\n_No actionable next steps._\n" in text


def test_code_fence_language_from_extension():
    assert report.language_for("src/auth.py") == "python"
    assert report.language_for("web/app.TSX") == "tsx"
    assert report.language_for("scripts/run.sh") == "bash"
    assert report.language_for("Makefile") == ""
    assert report.language_for("notes.unknownext") == ""
    py = block(norm(fix_start_line=1, fix_end_line=1, replacement="x = 1", file="src/a.py"))
    assert "```python\nx = 1\n```" in py
    bare = block(norm(fix_start_line=1, fix_end_line=1, replacement="x = 1", file="LICENSE"))
    assert "```\nx = 1\n```" in bare


def test_empty_replacement_renders_an_empty_block():
    text = block(norm(fix_start_line=3, fix_end_line=4, replacement=""))
    assert "```python\n\n```" in text


def test_fence_for_is_three_backticks_for_plain_code():
    assert report.fence_for("x = 1") == "```"
    assert report.fence_for("") == "```"
    assert report.fence_for("uses `inline` ticks and a ``` mid-line") == "```"


def test_fence_for_outgrows_backticks_in_replacement():
    assert report.fence_for("a\n```\nb") == "````"
    assert report.fence_for("a\n```python\nb\n````\n") == "`````"
    assert report.fence_for("  ```") == "````"  # an indented fence closes a fence too


def test_report_code_block_uses_a_longer_fence_when_needed():
    replacement = "print('''\n```\nhi\n```\n''')"
    text = block(norm(fix_start_line=1, fix_end_line=5, replacement=replacement, file="docs/a.md"))
    assert f"````markdown\n{replacement}\n````" in text


def test_location_code_span_outgrows_backticks_in_the_path():
    finding = norm(file="src/we`ird.py", line=42, fix="Rename it.")
    text = report.render_report([finding], REPO, SHA)
    label = "``src/we`ird.py:42``"
    assert f"[{label}](" in text
    assert f"{label} — Rename it." in text
    edge = block(norm(file="`x.py", line=42), "", "")
    assert "`` `x.py:42 ``" in edge  # a path starting with a backtick is padded

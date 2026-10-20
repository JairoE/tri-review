import json

import pytest

from tri_review.errors import InsufficientReviewsError
from tri_review.nodes import synthesize_node
from tri_review.schema import Finding, ReviewResult, SynthesisOutput


class _Raw:
    """Stands in for the raw AIMessage `include_raw=True` hands back."""

    usage_metadata = None


_NO_FINDINGS = SynthesisOutput()

# CapturingLLM's default reply: every finding it was sent; see _each_finding.
_EACH_FINDING = object()


class CapturingLLM:
    """Stands in for the synthesizer model, recording what it is sent.

    Asked for structured output, it answers the way
    `with_structured_output(schema, include_raw=True)` does, as
    `tests/test_nodes.py::FakeLLM` does for reviewers. Unless given an
    `output`, it lists every finding it was sent, as a synthesizer that drops
    nothing would: an empty reply to reviews that found issues is a failed
    synthesis. Invoked bare, it answers in prose like any chat model. That
    prose must never reach the report: tri-review renders the report itself.
    """

    PROSE = "## Consensus Findings\n\nThe md5 hash."

    def __init__(self, output=_EACH_FINDING, raises=None, parsing_error=None):
        self.output = output
        self.raises = raises
        self.parsing_error = parsing_error
        self.seen_payload = None
        # (schema, include_raw) once structured output has been asked for.
        self.structured = None

    def with_structured_output(self, schema, include_raw=False):
        self.structured = (schema, include_raw)
        return self

    def invoke(self, messages):
        if self.raises:
            raise self.raises
        self.seen_payload = messages[-1].content
        if self.structured is None:
            return type("Resp", (), {"content": self.PROSE})()
        output = _each_finding(self.seen_payload) if self.output is _EACH_FINDING else self.output
        return {"raw": _Raw(), "parsed": output, "parsing_error": self.parsing_error}


def _each_finding(payload: str) -> SynthesisOutput:
    """Each reviewer finding in `payload` once, as an Optional finding of its model alone."""
    return _synthesis(
        *(
            _report_finding(
                reported_by=[review["model"]],
                severity=finding["severity"],
                category=finding["category"],
                file=finding["file"],
                line=finding["line"],
                title=finding["title"],
                problem=finding["detail"],
                action="optional",
            )
            for review in json.loads(payload)
            for finding in review["findings"]
        )
    )


def _report_finding(**overrides) -> dict:
    """One synthesized finding as the model returns it: m1 and m2 agree, critical, Blocking."""
    base = dict(
        reported_by=["m1", "m2"],
        severity="critical",
        category="security",
        file="auth.py",
        line=4,
        title="md5 is not a password hash",
        problem="Passwords are stored as unsalted md5.",
        trigger="Any leak of the users table.",
        fix="Hash with bcrypt.",
        action="blocking",
        falsified_by="The hash never stores a password.",
    )
    base.update(overrides)
    return base


def _synthesis(*findings: dict) -> SynthesisOutput:
    """The structured reply, validated the way LangChain's parser validates it."""
    return SynthesisOutput.model_validate({"findings": list(findings)})


def _agreeing() -> dict:
    return {"results": [_result("m1", "md5 hashing"), _result("m2", "md5 hashing")]}


def _result(model, *titles, error=None):
    findings = [
        Finding(
            file="auth.py",
            line=4,
            severity="critical",
            category="security",
            title=t,
            detail=f"detail for {t}",
        )
        for t in titles
    ]
    return ReviewResult(model=model, findings=findings, error=error)


def test_requires_two_successful_reviews():
    state = {"results": [_result("m1", "a"), _result("m2", error="boom")]}
    with pytest.raises(InsufficientReviewsError, match="nothing to triangulate"):
        synthesize_node(state, llm_builder=lambda _: CapturingLLM())


def test_zero_successes_reports_every_failure():
    state = {
        "results": [
            _result("m1", error="no key"),
            _result("m2", error="timeout"),
        ]
    }
    with pytest.raises(InsufficientReviewsError) as exc:
        synthesize_node(state, llm_builder=lambda _: CapturingLLM())
    assert "no key" in str(exc.value)
    assert "timeout" in str(exc.value)


def test_two_successes_proceed_and_note_the_failure():
    llm = CapturingLLM()
    state = {
        "results": [
            _result("m1", "md5 hashing"),
            _result("m2", "md5 hashing"),
            _result("m3", error="rate limited"),
        ]
    }
    report = synthesize_node(state, llm_builder=lambda _: llm)["final_report"]
    assert "1 model(s) did not report" in report
    assert "m3" in report and "rate limited" in report
    assert "## Consensus Findings" in report


def test_all_three_succeed_has_no_failure_note():
    llm = CapturingLLM()
    state = {"results": [_result(m, "issue") for m in ("m1", "m2", "m3")]}
    report = synthesize_node(state, llm_builder=lambda _: llm)["final_report"]
    assert "did not report" not in report


def test_synthesizer_receives_structured_findings_from_every_model():
    llm = CapturingLLM()
    state = {"results": [_result("m1", "alpha"), _result("m2", "beta")]}
    synthesize_node(state, llm_builder=lambda _: llm)

    payload = json.loads(llm.seen_payload)
    assert {entry["model"] for entry in payload} == {"m1", "m2"}
    # file/line/severity are what make cross-model matching mechanical
    finding = payload[0]["findings"][0]
    assert finding["file"] == "auth.py"
    assert finding["line"] == 4
    assert finding["severity"] == "critical"


def test_single_provider_panel_is_disclosed():
    """Three OpenAI models agreeing is one opinion, and the report must say so."""
    state = {"results": [_result(m, "issue") for m in ("gpt-5.1", "gpt-4.1", "gpt-4o")]}
    report = synthesize_node(state, llm_builder=lambda _: CapturingLLM())["final_report"]
    assert "All 3 reviewers are `openai` models" in report
    assert "weaker evidence than cross-provider consensus" in report


def test_cross_provider_panel_gets_no_disclosure():
    state = {"results": [_result(m, "issue") for m in ("gpt-5.1", "claude-opus-5")]}
    report = synthesize_node(state, llm_builder=lambda _: CapturingLLM())["final_report"]
    assert "reviewers are" not in report


def test_disclosure_follows_the_models_that_actually_reported():
    """A failed reviewer can collapse a diverse panel into a single-provider one."""
    state = {
        "results": [
            _result("gpt-5.1", "issue"),
            _result("gpt-4.1", "issue"),
            _result("claude-opus-5", error="no key"),
        ]
    }
    report = synthesize_node(state, llm_builder=lambda _: CapturingLLM())["final_report"]
    assert "All 2 reviewers are `openai` models" in report


def test_synthesizer_failure_falls_back_to_raw_findings():
    llm = CapturingLLM(raises=RuntimeError("synth exploded"))
    state = {"results": [_result("m1", "alpha"), _result("m2", "beta")]}
    report = synthesize_node(state, llm_builder=lambda _: llm)["final_report"]
    assert "Synthesis failed" in report
    assert "alpha" in report and "beta" in report


def test_the_raw_fallback_still_shows_what_was_already_rejected():
    """#28: downgrade, never hide -- including when synthesis has failed."""
    from tri_review import dismissals

    llm = CapturingLLM(raises=RuntimeError("synth exploded"))
    state = {
        "results": [_result("m1", "the widget leaks"), _result("m2", "beta")],
        "dismissals": [
            dismissals.Dismissal(
                claim="the widget leaks", reason="checked, it does not", file="w.py"
            )
        ],
    }
    report = synthesize_node(state, llm_builder=lambda _: llm)["final_report"]
    assert "Synthesis failed" in report
    assert "### Previously rejected" in report
    assert "checked, it does not" in report
    # Listed after the findings, not merged into them.
    assert report.index("### Previously rejected") > report.index("beta")


def test_the_raw_fallback_is_unchanged_with_no_dismissals():
    llm = CapturingLLM(raises=RuntimeError("synth exploded"))
    state = {"results": [_result("m1", "alpha"), _result("m2", "beta")]}
    report = synthesize_node(state, llm_builder=lambda _: llm)["final_report"]
    assert "Previously rejected" not in report


def test_model_tagged_single_model_blocking_is_rendered_optional():
    """A model's Blocking tag is a claim that tri-review checks, not a fact it relays.

    Agreement alone is not evidence, and one model is not even agreement.
    This rule used to exist only as SYNTHESIS_PROMPT wording, which models
    did not always follow: two models once agreed that passing thinking_level
    to a legacy Gemini ID would fail, the step was promoted to Blocking on
    that agreement alone, and a direct check showed no such failure.
    """
    llm = CapturingLLM(output=_synthesis(_report_finding(reported_by=["m1"])))
    out = synthesize_node(_agreeing(), llm_builder=lambda _: llm)
    report = out["final_report"]
    assert "_Optional — only one model reported this_" in report
    assert "- **Optional** F1 · `auth.py:4`" in report
    assert "**Blocking**" not in report
    assert [f.action for f in out["synthesis"]] == ["optional"]


def test_pending_check_without_check_renders_optional():
    """An unobservable claim has to arrive with the check that would settle it.

    The failure mode is not that the model raises an unverifiable concern --
    it is that the concern arrives as a required change with nothing the
    reader can do to resolve it in one step.
    """
    unnamed = _report_finding(action="blocking_pending_check", check=None)
    report = synthesize_node(
        _agreeing(), llm_builder=lambda _: CapturingLLM(output=_synthesis(unnamed))
    )["final_report"]
    assert "_Optional — no check named_" in report
    assert "- **Optional** F1 · `auth.py:4`" in report
    assert "Blocking pending check" not in report

    named = _report_finding(action="blocking_pending_check", check="Hash a known password.")
    report = synthesize_node(
        _agreeing(), llm_builder=lambda _: CapturingLLM(output=_synthesis(named))
    )["final_report"]
    assert "- **Blocking pending check** F1 · `auth.py:4`" in report
    assert "Check: Hash a known password." in report


def test_every_rendered_finding_carries_its_severity():
    """Severity labels in the report must not depend on the synthesizer's whim.

    When the prompt asked for them, the same synthesizer on the same pinned
    version labelled every Unique Insight on one run and none on the next.
    tri-review now writes every heading itself, from the `severity` field.
    """
    findings = [
        _report_finding(title="agreed"),
        _report_finding(title="solo", reported_by=["m2"], severity="major", action="optional"),
        _report_finding(title="extra", reported_by=[], severity="minor", action="optional"),
    ]
    report = synthesize_node(
        _agreeing(), llm_builder=lambda _: CapturingLLM(output=_synthesis(*findings))
    )["final_report"]
    headings = [line for line in report.splitlines() if line.startswith("### ")]
    assert headings == [
        "### F1 · **critical** · security — agreed",
        "### F2 · **major** · security — solo",
        "### F3 · **minor** · security — extra",
    ]


def test_recorded_dismissals_reach_the_synthesizer():
    """A dismissal is useless if it never gets in front of the model."""
    from tri_review import dismissals, nodes

    seen = []

    class Recorder(CapturingLLM):
        def invoke(self, messages):
            seen.extend(m.content for m in messages)
            return super().invoke(messages)

    nodes.synthesize_node(
        {
            "results": [
                ReviewResult(model="a", findings=[Finding(
                    file="w.py", severity="major", category="bug",
                    title="the widget leaks", detail="d")]),
                ReviewResult(model="b", findings=[]),
            ],
            "dismissals": [
                dismissals.Dismissal(claim="the widget leaks", reason="checked, it does not")
            ],
        },
        llm_builder=lambda _: Recorder(),
    )
    joined = "\n".join(seen)
    assert "the widget leaks" in joined
    assert "checked, it does not" in joined


def test_no_dismissals_adds_no_message():
    """An empty ledger must leave the synthesizer's input exactly as it was."""
    from tri_review import nodes

    counts = []

    class Counter(CapturingLLM):
        def invoke(self, messages):
            counts.append(len(messages))
            return super().invoke(messages)

    nodes.synthesize_node(
        {"results": [ReviewResult(model="a", findings=[]), ReviewResult(model="b", findings=[])]},
        llm_builder=lambda _: Counter(),
    )
    assert counts == [2]  # system prompt + payload, nothing injected


def test_reviewers_never_receive_the_ledger():
    """Independence is the premise of triangulating three models.

    A dismissal must change how a finding is reported, never whether it is
    found -- so it may reach the synthesizer and must not reach a reviewer.
    """
    import inspect

    from tri_review import nodes

    assert "dismissals" not in inspect.getsource(nodes.review_with)


def test_malformed_findings_are_disclosed_above_the_report():
    partial = _result("m1", "a")
    partial.malformed_findings = ["findings[1] (auth.py): severity: bad value"]
    state = {"results": [partial, _result("m2", "b")]}
    report = synthesize_node(state, llm_builder=lambda _: CapturingLLM())["final_report"]
    assert "did not match the schema" in report
    assert "`m1`: 1 of 2 set aside -- findings[1] (auth.py)" in report


def test_no_malformed_findings_adds_no_note():
    state = {"results": [_result("m1", "a"), _result("m2", "b")]}
    report = synthesize_node(state, llm_builder=lambda _: CapturingLLM())["final_report"]
    assert "did not match the schema" not in report


# --- choosing the synthesizer -----------------------------------------------


def test_the_named_synthesizer_is_the_one_built():
    built = []
    state = {"results": [_result("m1", "a"), _result("m2", "b")]}
    synthesize_node(
        state,
        llm_builder=lambda spec: built.append(spec) or CapturingLLM(),
        synthesizer="gpt-astra@medium",
    )
    assert built == ["gpt-astra@medium"]


def test_the_synthesizer_need_not_be_a_reviewer():
    state = {"results": [_result("gpt-6-sol@high", "a"), _result("gpt-5.6-sol@high", "b")]}
    report = synthesize_node(
        state, llm_builder=lambda _: CapturingLLM(), synthesizer="gpt-astra@medium"
    )["final_report"]
    assert "Synthesizer: `gpt-astra@medium`" in report


def test_without_a_named_synthesizer_the_first_reviewer_writes_the_report(monkeypatch):
    """Not a hidden config slot: that synthesized a flag-picked panel with a model
    the user never named, and possibly had no key for."""
    monkeypatch.delenv("TRI_REVIEW_SYNTHESIZER", raising=False)
    built = []
    state = {"results": [_result("gpt-6-sol", "a"), _result("gpt-5.6-sol", "b")]}
    synthesize_node(state, llm_builder=lambda spec: built.append(spec) or CapturingLLM())
    assert built == ["gpt-6-sol"]


def test_the_env_var_still_names_a_standing_synthesizer(monkeypatch):
    monkeypatch.setenv("TRI_REVIEW_SYNTHESIZER", "claude-opus-5@high")
    built = []
    state = {"results": [_result("m1", "a"), _result("m2", "b")]}
    synthesize_node(state, llm_builder=lambda spec: built.append(spec) or CapturingLLM())
    assert built == ["claude-opus-5@high"]


def test_the_report_names_every_reviewer_including_the_ones_that_failed():
    state = {
        "results": [
            _result("gpt-6-sol@high", "a"),
            _result("gpt-5.6-sol@high", "b"),
            _result("claude-opus-5", error="no key"),
        ]
    }
    report = synthesize_node(
        state, llm_builder=lambda _: CapturingLLM(), synthesizer="gpt-astra@medium"
    )["final_report"]
    first_line = report.splitlines()[0]
    for spec in ("gpt-6-sol@high", "gpt-5.6-sol@high", "claude-opus-5", "gpt-astra@medium"):
        assert f"`{spec}`" in first_line


def test_the_panel_line_survives_a_failed_synthesis():
    llm = CapturingLLM(raises=RuntimeError("synth exploded"))
    state = {"results": [_result("m1", "a"), _result("m2", "b")]}
    report = synthesize_node(state, llm_builder=lambda _: llm, synthesizer="s")["final_report"]
    assert "Synthesizer: `s`" in report



# --- a panel of one -----------------------------------------------------------


def test_a_panel_of_one_is_reported_as_uncorroborated():
    state = {"results": [_result("gpt-6-sol@high", "a")]}
    report = synthesize_node(
        state, llm_builder=lambda _: CapturingLLM(), synthesizer="gpt-astra"
    )["final_report"]
    assert "Single reviewer: `gpt-6-sol@high`" in report
    assert "reviewers are" not in report, "one model is not a single-provider panel"


def test_a_panel_of_one_that_fails_is_still_insufficient():
    state = {"results": [_result("m1", error="no key")]}
    with pytest.raises(InsufficientReviewsError, match="Only 0 of 1"):
        synthesize_node(state, llm_builder=lambda _: CapturingLLM())


def test_a_larger_panel_that_degrades_to_one_is_still_insufficient():
    """Chosen as a triangulation and failed as one: exit 4, as before."""
    state = {
        "results": [
            _result("m1", "a"),
            _result("m2", error="down"),
            _result("m3", error="down"),
        ]
    }
    with pytest.raises(InsufficientReviewsError, match="Only 1 of 3"):
        synthesize_node(state, llm_builder=lambda _: CapturingLLM())


def test_no_results_at_all_is_insufficient():
    with pytest.raises(InsufficientReviewsError):
        synthesize_node({"results": []}, llm_builder=lambda _: CapturingLLM())


# --- review findings on PR #25, second round ---------------------------------


def test_a_successful_synthesis_says_so():
    state = {"results": [_result("m1", "a"), _result("m2", "b")]}
    out = synthesize_node(state, llm_builder=lambda _: CapturingLLM(), synthesizer="s")
    assert out["synthesized"] is True


def test_a_failed_synthesis_says_so_so_it_is_not_stored_as_the_answer():
    """The fallback report is shown, but a re-run must retry the synthesizer."""
    llm = CapturingLLM(raises=RuntimeError("credential validation failed"))
    state = {"results": [_result("m1", "a"), _result("m2", "b")]}
    out = synthesize_node(state, llm_builder=lambda _: llm, synthesizer="s")
    assert out["synthesized"] is False
    assert "Synthesis failed" in out["final_report"]


def test_the_panel_line_marks_reviewers_that_did_not_report():
    state = {
        "results": [
            _result("gpt-6-sol", "a"),
            _result("gpt-5.6-sol", "b"),
            _result("claude-opus-5", error="no key"),
        ]
    }
    report = synthesize_node(
        state, llm_builder=lambda _: CapturingLLM(), synthesizer="s"
    )["final_report"]
    first_line = report.splitlines()[0]
    assert "`claude-opus-5` (did not report)" in first_line
    assert "`gpt-6-sol` (did not report)" not in first_line


@pytest.mark.parametrize(
    "outcomes, required",
    [
        (["ok"], 1),
        (["ok", "ok"], 2),
        (["ok", "fail"], 2),
        (["ok", "fail", "fail"], 2),
        (["ok", "ok", "fail"], 2),
    ],
)
def test_one_of_one_or_two_of_anything_larger(outcomes, required):
    results = [
        _result(f"m{i}", "a") if o == "ok" else _result(f"m{i}", error="down")
        for i, o in enumerate(outcomes)
    ]
    succeeded = outcomes.count("ok")
    if succeeded >= required:
        synthesize_node({"results": results}, llm_builder=lambda _: CapturingLLM())
    else:
        with pytest.raises(InsufficientReviewsError):
            synthesize_node({"results": results}, llm_builder=lambda _: CapturingLLM())


# --- structured synthesis (R3, R11) -------------------------------------------


def test_synthesis_is_requested_as_structured_output():
    llm = CapturingLLM()
    synthesize_node(_agreeing(), llm_builder=lambda _: llm)
    assert llm.structured == (SynthesisOutput, True)


def test_report_is_rendered_by_tri_review_not_the_model():
    """The model returns findings; the layout, IDs and links are tri-review's (ADR 0001)."""
    llm = CapturingLLM(output=_synthesis(_report_finding()))
    state = {**_agreeing(), "link_repo": "o/r", "head_ref": "abc123"}
    out = synthesize_node(state, llm_builder=lambda _: llm)
    report = out["final_report"]
    assert (
        "## Consensus Findings\n\n### F1 · **critical** · security — md5 is not a password hash"
        in report
    )
    assert "[`auth.py:4`](https://github.com/o/r/blob/abc123/auth.py#L4) · `m1`, `m2`" in report
    assert "- **Blocking** F1 · `auth.py:4` — Hash with bcrypt." in report
    assert "The md5 hash." not in report
    assert out["synthesized"] is True
    assert [f.id for f in out["synthesis"]] == ["F1"]


def test_parsing_error_falls_back_to_raw_listing_in_block_format():
    """include_raw=True hands a parse failure back as a value, not an exception.

    Unchecked, it reads as a synthesis that found nothing, and is stored as
    this commit's answer.
    """
    llm = CapturingLLM(output=None, parsing_error=ValueError("not a SynthesisOutput"))
    state = {
        "results": [_result("m1", "alpha"), _result("m2", "beta")],
        "link_repo": "o/r",
        "head_ref": "abc123",
    }
    out = synthesize_node(state, llm_builder=lambda _: llm)
    report = out["final_report"]
    assert "Synthesis failed (ValueError: not a SynthesisOutput)" in report
    assert "### m1 (1 findings)\n\n#### **critical** · security — alpha\n" in report
    assert "[`auth.py:4`](https://github.com/o/r/blob/abc123/auth.py#L4)" in report
    assert "**Problem:** detail for alpha\n**Trigger:** _not stated_" in report
    assert "F1" not in report and "Actionable Next Steps" not in report
    assert out["synthesized"] is False


def test_all_malformed_synthesis_falls_back():
    """Nothing that validated is a failed synthesis, not an empty report (R3)."""
    bad = {**_report_finding(), "action": "urgent"}
    llm = CapturingLLM(output=_synthesis(bad, bad))
    out = synthesize_node(_agreeing(), llm_builder=lambda _: llm)
    report = out["final_report"]
    assert "Synthesis failed" in report
    assert "none matching the findings schema" in report
    assert "findings[0] (auth.py): action:" in report
    assert "#### **critical** · security — md5 hashing" in report
    assert out["synthesized"] is False


def test_malformed_synthesis_entries_are_disclosed_in_the_header():
    """One bad entry costs only itself, and the report says it was set aside (R3)."""
    bad = {**_report_finding(file="other.py"), "action": "urgent"}
    llm = CapturingLLM(output=_synthesis(_report_finding(), bad))
    out = synthesize_node(_agreeing(), llm_builder=lambda _: llm)
    report = out["final_report"]
    assert "The synthesizer returned 1 finding(s) that did not match the schema." in report
    assert "- findings[1] (other.py): action:" in report
    assert report.index("findings[1] (other.py)") < report.index("## Consensus Findings")
    assert out["synthesized"] is True
    assert [f.id for f in out["synthesis"]] == ["F1"]


@pytest.mark.parametrize(
    "make_llm",
    [
        pytest.param(lambda: CapturingLLM(raises=RuntimeError("synth exploded")), id="provider-error"),
        pytest.param(
            lambda: CapturingLLM(output=None, parsing_error=ValueError("bad tool call")),
            id="parsing-error",
        ),
        # A reply with no tool call parses to None with no error: no synthesis,
        # not an empty one.
        pytest.param(lambda: CapturingLLM(output=None), id="no-structured-output"),
        pytest.param(
            lambda: CapturingLLM(output=_synthesis({"action": "urgent"})), id="all-malformed"
        ),
    ],
)
def test_fallback_returns_empty_synthesis(make_llm):
    """The raw fallback has nothing to store or post as a synthesis."""
    llm = make_llm()
    out = synthesize_node(_agreeing(), llm_builder=lambda _: llm)
    assert "Synthesis failed" in out["final_report"]
    assert out["synthesized"] is False
    assert out["synthesis"] == []


def test_an_empty_synthesis_of_reviews_that_found_issues_falls_back():
    """An empty `findings` list says the reviews found nothing. These did not.

    Rendered as it stands, it is a clean synthesized report that hides every
    reviewer's finding behind "No consensus findings.", and it is stored as
    this commit's answer, so a re-run replays it instead of retrying. Like a
    reply with no tool call, it is no synthesis at all.
    """
    out = synthesize_node(_agreeing(), llm_builder=lambda _: CapturingLLM(output=_NO_FINDINGS))
    report = out["final_report"]
    assert "Synthesis failed (ValueError: returned no findings" in report
    assert "#### **critical** · security — md5 hashing" in report
    assert out["synthesized"] is False
    assert out["synthesis"] == []


def test_an_empty_synthesis_of_clean_reviews_is_a_clean_report():
    state = {"results": [ReviewResult(model="m1"), ReviewResult(model="m2")]}
    out = synthesize_node(state, llm_builder=lambda _: CapturingLLM(output=_NO_FINDINGS))
    assert "_No consensus findings._" in out["final_report"]
    assert out["synthesized"] is True
    assert out["synthesis"] == []


@pytest.mark.parametrize(
    "silent",
    [
        pytest.param(ReviewResult(model="m2"), id="clean-review"),
        pytest.param(
            ReviewResult(model="m2", low_confidence_reason="spent its output budget reasoning"),
            id="low-confidence-empty-review",
        ),
    ],
)
def test_a_reviewer_that_reported_nothing_does_not_make_consensus(silent):
    """A review with no findings reported no issue, so it is no issue's reporter.

    Counted, a synthesizer that lists it beside the one model that did report
    the issue turns a single-model claim into Consensus, and a Blocking tag
    survives on agreement nobody gave (constraint 6). The low-confidence note
    already tells the reader such a reviewer is no corroboration.
    """
    state = {"results": [_result("m1", "md5 hashing"), silent]}
    llm = CapturingLLM(output=_synthesis(_report_finding(reported_by=["m1", "m2"])))
    out = synthesize_node(state, llm_builder=lambda _: llm)
    (finding,) = out["synthesis"]
    assert (finding.kind, finding.reported_by, finding.action) == ("unique", ["m1"], "optional")
    report = out["final_report"]
    assert "_No consensus findings._" in report
    assert "_Optional — only one model reported this_" in report
    assert "- **Optional** F1 · `auth.py:4`" in report


_DIFF = (
    "diff --git a/auth.py b/auth.py\n"
    "--- a/auth.py\n"
    "+++ b/auth.py\n"
    "@@ -1,2 +1,5 @@\n"
    " import hashlib\n"
    "+def store(password):\n"
    "+    return hashlib.md5(password).hexdigest()\n"
    "+\n"
    " SALT = None\n"
)


_STORE_FIX = {
    "fix_start_line": 2,
    "fix_end_line": 3,
    "replacement": "def store(password):\n    return bcrypt.hashpw(password, bcrypt.gensalt())",
}


def _offering(model, file="auth.py", line=3, **fix):
    """`model`'s review of the md5 issue at `file`:`line`, offering `fix` when one is given."""
    return ReviewResult(
        model=model,
        findings=[
            Finding(
                file=file,
                line=line,
                severity="critical",
                category="security",
                title="md5 hashing",
                detail="Passwords are stored as unsalted md5.",
                trigger="Any leak of the users table.",
                suggested_fix="Hash with bcrypt.",
                **fix,
            )
        ],
    )


def test_applyability_is_set_from_the_diff_in_state():
    unrelated_fix = {"fix_start_line": 40, "fix_end_line": 40, "replacement": "x = 1"}
    inside = _report_finding(line=3, **_STORE_FIX)
    outside = _report_finding(title="unrelated", line=40, **unrelated_fix)
    # Each fix is copied from one of the finding's reporters, for that issue,
    # as R3 requires: m1 offered the first at line 3, m2 the second at line 40.
    state = {
        "results": [_offering("m1", **_STORE_FIX), _offering("m2", line=40, **unrelated_fix)],
        "diff": _DIFF,
    }
    llm = CapturingLLM(output=_synthesis(inside, outside))
    out = synthesize_node(state, llm_builder=lambda _: llm)
    report = out["final_report"]
    assert "_Inline suggestion posted on L2-3._" in report
    assert "_No inline suggestion: lines outside the diff._" in report
    first, second = out["synthesis"]
    assert (first.applyable, first.inline_reason) == (True, None)
    assert (first.fix_start_line, first.fix_end_line, first.replacement) == tuple(
        _STORE_FIX.values()
    )
    assert (second.applyable, second.inline_reason) == (False, "lines outside the diff")


@pytest.mark.parametrize(
    "results, fix, reported_by, line",
    [
        pytest.param(
            [_offering("m1"), _offering("m2")],
            {**_STORE_FIX, "replacement": "import os; os.system('rm -rf /')"},
            ["m1", "m2"],
            3,
            id="no-reviewer-offered-a-fix",
        ),
        pytest.param(
            [_offering("m1", **_STORE_FIX), _offering("m2")],
            {**_STORE_FIX, "replacement": "def store(password):\n    return password"},
            ["m1", "m2"],
            3,
            id="reporters-replacement-edited",
        ),
        pytest.param(
            [_offering("m1", **_STORE_FIX), _offering("m2")],
            {**_STORE_FIX, "fix_start_line": 3},
            ["m1", "m2"],
            3,
            id="reporters-range-moved",
        ),
        pytest.param(
            [_offering("m1", file="other.py", **_STORE_FIX), _offering("m2")],
            _STORE_FIX,
            ["m1", "m2"],
            3,
            id="offered-for-another-file",
        ),
        pytest.param(
            [_offering("m1", **_STORE_FIX), _offering("m2")],
            _STORE_FIX,
            ["m2"],
            3,
            id="offered-by-a-model-not-listed",
        ),
        pytest.param(
            [_offering("m1"), _offering("m2")],
            {"fix_start_line": 2, "fix_end_line": 3},
            ["m1", "m2"],
            3,
            id="range-no-reviewer-offered",
        ),
        pytest.param(
            # m1 offered this exact fix for its md5 finding at line 3; the
            # synthesized finding is a different issue at line 40, outside the
            # fix range, so the fix is not the reporters' fix for this issue.
            [_offering("m1", **_STORE_FIX), _offering("m2")],
            _STORE_FIX,
            ["m1", "m2"],
            40,
            id="reporters-fix-for-another-issue",
        ),
    ],
)
def test_a_fix_no_listed_reporter_offered_is_dropped(results, fix, reported_by, line):
    """The synthesizer copies a reporter's fix; it never writes one (R3).

    Passed through, a replacement no reviewer offered is checked against the
    diff like any other and posted as a one-click suggested change: the wrong
    change ADR 0001 exists to prevent. Only an exact copy of a fix that a
    listed reporter offered for the same file and issue survives; anything else loses
    all three fix fields, so neither the code nor its range reaches the report.
    """
    invented = _report_finding(line=line, reported_by=reported_by, **fix)
    llm = CapturingLLM(output=_synthesis(invented))
    out = synthesize_node({"results": results, "diff": _DIFF}, llm_builder=lambda _: llm)
    (finding,) = out["synthesis"]
    assert finding.applyable is False
    assert (finding.fix_start_line, finding.fix_end_line, finding.replacement) == (None,) * 3
    report = out["final_report"]
    assert "Inline suggestion posted" not in report
    assert "```" not in report, "the dropped replacement must not be rendered"
    assert f"`auth.py:{line}` ·" in report, "the location falls back to the finding's line"
    assert out["synthesized"] is True

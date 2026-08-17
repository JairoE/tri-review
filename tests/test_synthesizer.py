import json

import pytest

from tri_review.errors import InsufficientReviewsError
from tri_review.nodes import synthesize_node
from tri_review.schema import Finding, ReviewResult


class CapturingLLM:
    def __init__(self, reply="## Consensus Findings\n\nThe md5 hash.", raises=None):
        self.reply = reply
        self.raises = raises
        self.seen_payload = None

    def invoke(self, messages):
        if self.raises:
            raise self.raises
        self.seen_payload = messages[-1].content
        return type("Resp", (), {"content": self.reply})()


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


def test_handles_content_block_list_responses():
    class BlockLLM(CapturingLLM):
        def invoke(self, messages):
            return type("Resp", (), {"content": [{"type": "text", "text": "block report"}]})()

    state = {"results": [_result("m1", "a"), _result("m2", "b")]}
    report = synthesize_node(state, llm_builder=lambda _: BlockLLM())["final_report"]
    assert "block report" in report

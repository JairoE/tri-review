import pytest

from tri_review.nodes import build_llm, make_review_node, review_with
from tri_review.schema import Finding, ReviewOutput


class FakeLLM:
    def __init__(self, output=None, raises=None):
        self._output = output
        self._raises = raises

    def with_structured_output(self, _schema):
        return self

    def invoke(self, _messages):
        if self._raises:
            raise self._raises
        return self._output


def _finding(**kw):
    base = dict(
        file="src/app.py",
        line=10,
        severity="major",
        category="bug",
        title="Off-by-one",
        detail="Loop runs one iteration too many.",
    )
    base.update(kw)
    return Finding(**base)


def test_successful_review_returns_findings():
    llm = FakeLLM(output=ReviewOutput(findings=[_finding()]))
    result = review_with("fake-model", "payload", llm_builder=lambda _: llm)
    assert result.ok
    assert result.model == "fake-model"
    assert result.findings[0].title == "Off-by-one"


def test_clean_diff_yields_empty_findings():
    llm = FakeLLM(output=ReviewOutput(findings=[]))
    result = review_with("fake-model", "payload", llm_builder=lambda _: llm)
    assert result.ok
    assert result.findings == []


def test_llm_exception_is_recorded_not_raised():
    llm = FakeLLM(raises=RuntimeError("provider exploded"))
    result = review_with("fake-model", "payload", llm_builder=lambda _: llm)
    assert not result.ok
    assert "provider exploded" in result.error
    assert result.findings == []


def test_timeout_is_recorded_not_raised():
    llm = FakeLLM(raises=TimeoutError("timed out after 120s"))
    result = review_with("fake-model", "payload", llm_builder=lambda _: llm)
    assert not result.ok
    assert "TimeoutError" in result.error


def test_builder_failure_is_recorded_not_raised():
    def exploding_builder(_name):
        raise ValueError("missing API key")

    result = review_with("fake-model", "payload", llm_builder=exploding_builder)
    assert not result.ok
    assert "missing API key" in result.error


def test_none_output_is_treated_as_no_findings():
    llm = FakeLLM(output=None)
    result = review_with("fake-model", "payload", llm_builder=lambda _: llm)
    assert result.ok
    assert result.findings == []


def test_node_appends_single_result_to_state():
    llm = FakeLLM(output=ReviewOutput(findings=[_finding()]))
    node = make_review_node("fake-model", llm_builder=lambda _: llm)
    update = node({"payload": "diff", "results": []})
    assert len(update["results"]) == 1
    assert update["results"][0].model == "fake-model"


def test_unknown_model_id_is_rejected():
    with pytest.raises(ValueError, match="Unrecognized model ID"):
        build_llm("llama-9000")

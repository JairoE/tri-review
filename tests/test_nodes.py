import pytest

from tri_review.nodes import make_review_node, review_with
from tri_review.providers import build_llm, provider_of
from tri_review.schema import Finding, ReviewOutput


class _FakeRaw:
    """Stands in for the raw AIMessage `include_raw=True` hands back."""

    def __init__(self, usage_metadata=None):
        self.usage_metadata = usage_metadata


class FakeLLM:
    def __init__(self, output=None, raises=None, usage_metadata=None, parsing_error=None):
        self._output = output
        self._raises = raises
        self._usage_metadata = usage_metadata
        self._parsing_error = parsing_error

    def with_structured_output(self, _schema, include_raw=False):
        return self

    def invoke(self, _messages):
        if self._raises:
            raise self._raises
        return {
            "raw": _FakeRaw(self._usage_metadata),
            "parsed": self._output,
            "parsing_error": self._parsing_error,
        }


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


def test_chained_exception_cause_is_recorded():
    """A generic-message wrapper (e.g. anthropic.APIConnectionError -> "Connection
    error.") must not swallow the real underlying failure on `__cause__`."""

    def _raise_wrapped():
        try:
            raise OSError("Name or service not known")
        except OSError as cause:
            raise ConnectionError("Connection error.") from cause

    try:
        _raise_wrapped()
    except ConnectionError as wrapped:
        llm = FakeLLM(raises=wrapped)

    result = review_with("fake-model", "payload", llm_builder=lambda _: llm)
    assert not result.ok
    assert "Connection error." in result.error
    assert "Name or service not known" in result.error


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


def test_empty_findings_after_reasoning_exhaustion_is_flagged_low_confidence():
    """A schema-valid empty answer that ate almost the whole output budget on
    reasoning must not be reported as an ordinary clean pass."""
    llm = FakeLLM(
        output=ReviewOutput(findings=[]),
        usage_metadata={"output_tokens": 6907, "output_token_details": {"reasoning": 6898}},
    )
    result = review_with("fake-model", "payload", llm_builder=lambda _: llm)
    assert result.ok
    assert result.findings == []
    assert result.low_confidence_reason is not None
    assert "6898" in result.low_confidence_reason


def test_genuine_clean_diff_is_not_flagged_low_confidence():
    """No reasoning-heavy usage metadata at all -- an ordinary clean pass."""
    llm = FakeLLM(output=ReviewOutput(findings=[]))
    result = review_with("fake-model", "payload", llm_builder=lambda _: llm)
    assert result.ok
    assert result.findings == []
    assert result.low_confidence_reason is None


def test_light_reasoning_then_genuinely_clean_is_not_flagged_low_confidence():
    """A small leftover-answer ratio alone must not flag this -- {"findings": []}
    is only ever a handful of tokens whether or not anything went wrong. Only a
    model that burned a substantial reasoning budget and still landed empty
    should be flagged."""
    llm = FakeLLM(
        output=ReviewOutput(findings=[]),
        usage_metadata={"output_tokens": 59, "output_token_details": {"reasoning": 50}},
    )
    result = review_with("fake-model", "payload", llm_builder=lambda _: llm)
    assert result.ok
    assert result.low_confidence_reason is None


def test_inconsistent_usage_metadata_is_not_flagged_low_confidence():
    """Reasoning tokens exceeding output tokens is nonsensical provider data,
    not evidence of anything -- must not produce a misleading message."""
    llm = FakeLLM(
        output=ReviewOutput(findings=[]),
        usage_metadata={"output_tokens": 10, "output_token_details": {"reasoning": 50}},
    )
    result = review_with("fake-model", "payload", llm_builder=lambda _: llm)
    assert result.ok
    assert result.low_confidence_reason is None


def test_malformed_usage_metadata_shape_does_not_fail_an_otherwise_ok_review():
    """A non-dict usage_metadata (an unexpected provider/SDK shape) must not
    raise out of the low-confidence check and turn a successful empty review
    into a reported failure."""
    llm = FakeLLM(output=ReviewOutput(findings=[]), usage_metadata="not-a-dict")
    result = review_with("fake-model", "payload", llm_builder=lambda _: llm)
    assert result.ok
    assert result.findings == []
    assert result.low_confidence_reason is None


def test_parsing_failure_is_recorded_as_an_error_not_cached_as_empty_findings():
    """include_raw=True lets a parse failure land in `parsing_error` without
    raising -- this must still surface as a real failure, not a silent clean
    pass that then gets cached as one."""
    builder = _CountingBuilder()
    llm = FakeLLM(parsing_error=ValueError("could not parse model output as ReviewOutput"))
    result = review_with("fake-model", "payload", llm_builder=lambda _: llm)
    assert not result.ok
    assert "could not parse model output" in result.error
    assert result.findings == []

    # And a parsing failure must not be cached as a false "clean" answer --
    # the next call has to actually retry, not replay the same broken result.
    retried = review_with("fake-model", "payload", llm_builder=builder)
    assert builder.calls == 1
    assert retried.ok


def test_real_findings_are_never_flagged_low_confidence_even_with_heavy_reasoning():
    """A finding means the model demonstrably did the work -- reasoning ratio is moot."""
    llm = FakeLLM(
        output=ReviewOutput(findings=[_finding()]),
        usage_metadata={"output_tokens": 6907, "output_token_details": {"reasoning": 6898}},
    )
    result = review_with("fake-model", "payload", llm_builder=lambda _: llm)
    assert result.ok
    assert result.low_confidence_reason is None


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


@pytest.mark.parametrize(
    "model_name, expected",
    [
        ("gpt-5.1", "openai"),
        ("o3-mini", "openai"),
        ("claude-opus-5", "anthropic"),
        ("gemini-3.1-pro-preview", "google"),
        ("llama-9000", None),
    ],
)
def test_provider_is_read_from_the_model_id(model_name, expected):
    assert provider_of(model_name) == expected


# --- the exact reviewer cache ----------------------------------------------


class _CountingBuilder:
    """Builds an LLM and records how many times a provider was actually reached."""

    def __init__(self, output=None, raises=None, usage_metadata=None):
        self.calls = 0
        self._output = output
        self._raises = raises
        self._usage_metadata = usage_metadata

    def __call__(self, _model):
        self.calls += 1
        return FakeLLM(output=self._output, raises=self._raises, usage_metadata=self._usage_metadata)


def test_an_identical_call_is_not_bought_twice():
    builder = _CountingBuilder(output=ReviewOutput(findings=[_finding()]))

    first = review_with("cache-model", "same payload", llm_builder=builder)
    second = review_with("cache-model", "same payload", llm_builder=builder)

    assert builder.calls == 1
    assert second.cached is True and first.cached is False
    assert second.findings[0].title == first.findings[0].title


def test_a_changed_payload_is_a_different_question():
    """One line different is a different review, however similar it looks."""
    builder = _CountingBuilder(output=ReviewOutput(findings=[_finding()]))

    review_with("cache-model", "payload v1", llm_builder=builder)
    review_with("cache-model", "payload v2", llm_builder=builder)

    assert builder.calls == 2


def test_a_different_model_does_not_inherit_another_models_opinion():
    builder = _CountingBuilder(output=ReviewOutput(findings=[_finding()]))

    review_with("model-a", "same payload", llm_builder=builder)
    review_with("model-b", "same payload", llm_builder=builder)

    assert builder.calls == 2


def test_a_failure_is_retried_rather_than_replayed():
    """The point of the cache is that a flaked provider is the only one re-called."""
    failing = _CountingBuilder(raises=RuntimeError("provider exploded"))

    first = review_with("flaky-model", "payload", llm_builder=failing)
    second = review_with("flaky-model", "payload", llm_builder=failing)

    assert not first.ok and not second.ok
    assert failing.calls == 2


def test_a_retry_after_a_flake_reuses_the_reviews_already_paid_for():
    """Two models landed, one did not. The retry must only re-call the one."""
    good = _CountingBuilder(output=ReviewOutput(findings=[_finding()]))
    bad = _CountingBuilder(raises=RuntimeError("connection error"))

    review_with("steady-model", "shared payload", llm_builder=good)
    review_with("flaky-model", "shared payload", llm_builder=bad)

    review_with("steady-model", "shared payload", llm_builder=good)
    review_with("flaky-model", "shared payload", llm_builder=bad)

    assert good.calls == 1
    assert bad.calls == 2


def test_the_cache_can_be_switched_off():
    builder = _CountingBuilder(output=ReviewOutput(findings=[_finding()]))

    review_with("cache-model", "payload", llm_builder=builder)
    review_with("cache-model", "payload", llm_builder=builder, use_cache=False)

    assert builder.calls == 2


def test_review_nodes_pass_the_cache_setting_through():
    builder = _CountingBuilder(output=ReviewOutput(findings=[_finding()]))
    state = {"payload": "node payload"}

    make_review_node("node-model", builder, use_cache=False)(state)
    make_review_node("node-model", builder, use_cache=False)(state)

    assert builder.calls == 2

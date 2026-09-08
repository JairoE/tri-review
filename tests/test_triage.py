"""Triage is the one gate that can be wrong, so every ambiguity must favour reviewing."""

from tri_review import triage
from tri_review.triage import TriageVerdict


class _LLM:
    def __init__(self, returns=None, raises=None):
        self._returns = returns
        self._raises = raises

    def with_structured_output(self, _schema):
        if self._raises is not None:
            raise self._raises
        return self

    def invoke(self, _messages):
        return self._returns


def test_a_provider_failure_never_blocks_the_review():
    """No key, no network, rate limit -- all of it must fall through to reviewing."""
    verdict = triage.assess("diff", llm_builder=lambda _: _LLM(raises=RuntimeError("no key")))
    assert verdict is None


def test_an_unparseable_answer_is_treated_as_no_answer():
    verdict = triage.assess("diff", llm_builder=lambda _: _LLM(returns="sure, looks fine"))
    assert verdict is None


def test_a_none_answer_is_treated_as_no_answer():
    assert triage.assess("diff", llm_builder=lambda _: _LLM(returns=None)) is None


def test_a_clean_verdict_is_passed_through():
    verdict = triage.assess(
        "diff",
        llm_builder=lambda _: _LLM(
            returns=TriageVerdict(changes_behavior=False, reason="comments only")
        ),
    )
    assert verdict is not None and verdict.changes_behavior is False


def test_the_prompt_names_comments_that_are_not_inert():
    """The exact failure mode of naive comment-stripping, so it must stay named."""
    prompt = triage.TRIAGE_PROMPT
    for directive in ("# noqa", "type: ignore", "eslint-disable", "#!/usr/bin/env"):
        assert directive in prompt


def test_the_prompt_tells_the_model_which_way_to_err():
    assert "answer true" in triage.TRIAGE_PROMPT.lower()

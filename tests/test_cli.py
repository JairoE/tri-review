import click
import pytest
from click.testing import CliRunner

from tri_review import config
from tri_review.cli import _resolve_models, main


def test_defaults_to_the_three_configured_slots():
    assert _resolve_models(()) == [config.model_a(), config.model_b(), config.model_c()]


def test_env_overrides_still_apply_when_no_flag_given(monkeypatch):
    monkeypatch.setenv("TRI_REVIEW_MODEL_A", "gpt-4o")
    assert _resolve_models(())[0] == "gpt-4o"


def test_selected_models_replace_the_defaults():
    chosen = ("gpt-5.1", "claude-opus-5", "gemini-2.5-pro")
    assert _resolve_models(chosen) == list(chosen)


def test_two_models_are_enough():
    assert _resolve_models(("gpt-5.1", "claude-opus-5")) == ["gpt-5.1", "claude-opus-5"]


def test_more_than_three_models_are_allowed():
    chosen = ("gpt-5.1", "gpt-4.1", "claude-opus-5", "gemini-2.5-pro")
    assert _resolve_models(chosen) == list(chosen)


def test_single_model_is_rejected():
    with pytest.raises(click.BadParameter, match="at least 2 models"):
        _resolve_models(("gpt-5.1",))


def test_unknown_model_id_is_rejected_up_front():
    with pytest.raises(click.BadParameter, match="unrecognized model ID"):
        _resolve_models(("gpt-5.1", "llama-9000"))


def test_rejection_message_names_every_bad_id():
    with pytest.raises(click.BadParameter) as exc:
        _resolve_models(("llama-9000", "mistral-large", "gpt-5.1"))
    message = str(exc.value)
    assert "llama-9000" in message
    assert "mistral-large" in message


def test_duplicate_models_are_left_alone():
    """Two runs of the same model is a legitimate (if odd) self-consistency check."""
    assert _resolve_models(("gpt-5.1", "gpt-5.1")) == ["gpt-5.1", "gpt-5.1"]


def test_help_documents_the_flag():
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "--model" in result.output


def test_bad_flag_exits_before_touching_github(monkeypatch):
    """A typo must fail fast, not after fetching the PR."""
    called = []
    monkeypatch.setattr("tri_review.github.preflight", lambda: called.append("preflight"))

    result = CliRunner().invoke(main, ["--pr", "1", "--model", "llama-9000", "--model", "gpt-5.1"])

    assert result.exit_code != 0
    assert called == []

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
    chosen = ("gpt-5.1", "claude-opus-5", "gemini-3.1-pro-preview")
    assert _resolve_models(chosen) == list(chosen)


def test_two_models_are_enough():
    assert _resolve_models(("gpt-5.1", "claude-opus-5")) == ["gpt-5.1", "claude-opus-5"]


def test_more_than_three_models_are_allowed():
    chosen = ("gpt-5.1", "gpt-4.1", "claude-opus-5", "gemini-3.1-pro-preview")
    assert _resolve_models(chosen) == list(chosen)


def test_single_model_is_rejected():
    with pytest.raises(click.BadParameter, match="at least 2 distinct models"):
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


def test_duplicate_models_are_collapsed():
    """The same model twice would pay for one opinion and report it as consensus."""
    assert _resolve_models(("gpt-5.1", "gpt-5.1", "claude-opus-5")) == [
        "gpt-5.1",
        "claude-opus-5",
    ]


def test_the_same_model_twice_is_not_a_panel():
    with pytest.raises(click.BadParameter, match="at least 2 distinct models"):
        _resolve_models(("gpt-5.1", "gpt-5.1"))


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


def test_bracketed_paths_survive_the_context_printout(capsys):
    """A path like `app/reviews/[id]/page.tsx` must print whole.

    Rich reads `[id]` as a style tag and silently drops it, so every Next.js
    dynamic route was being reported under a path that does not exist. Diff
    paths are arbitrary text and must be escaped before they reach markup.
    """
    from tri_review.cli import _print_context
    from tri_review.context import ReviewContext

    ctx = ReviewContext(
        diff="diff --git a/x b/x\n",
        files={"web/app/reviews/[id]/page.tsx": "x"},
        dropped=["web/app/[slug]/page.tsx"],
        missing=["pkg/[a]/b.ts"],
        rejected=["../[evil]/x"],
    )
    _print_context("1", ctx)

    out = capsys.readouterr().out
    assert "web/app/reviews/[id]/page.tsx" in out
    assert "web/app/[slug]/page.tsx" in out
    assert "pkg/[a]/b.ts" in out
    assert "../[evil]/x" in out


def test_provider_errors_survive_the_result_printout(capsys):
    """Provider messages are arbitrary text too, and often contain brackets."""
    from tri_review.cli import _print_result
    from tri_review.schema import ReviewResult

    _print_result(ReviewResult(model="m1", error="BadRequest: [invalid_request] too long"))

    assert "[invalid_request]" in capsys.readouterr().out

import click
import pytest
from click.testing import CliRunner

from tri_review import config
from tri_review.cli import _merge_url, _resolve_models, main


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


# --- --url reconciliation ---------------------------------------------------

URL = "https://github.com/octocat/Hello-World/pull/10856"


def test_url_alone_supplies_repo_and_pr():
    assert _merge_url(URL, None, None) == ("octocat/Hello-World", "10856")


def test_url_agreeing_with_explicit_flags_is_accepted():
    assert _merge_url(URL, "octocat/Hello-World", "10856") == (
        "octocat/Hello-World",
        "10856",
    )


def test_url_conflicting_with_repo_is_rejected():
    """Silently preferring the URL would review a repo the user did not ask for."""
    with pytest.raises(click.BadParameter) as exc:
        _merge_url(URL, "someone/else", None)
    assert "octocat/Hello-World" in str(exc.value)
    assert "someone/else" in str(exc.value)


def test_url_conflicting_with_pr_is_rejected():
    with pytest.raises(click.BadParameter) as exc:
        _merge_url(URL, None, "999")
    assert "10856" in str(exc.value)
    assert "999" in str(exc.value)


def test_conflicting_url_exits_before_touching_github(monkeypatch):
    """Like a model typo, a contradiction must fail before the PR is fetched."""
    called = []
    monkeypatch.setattr(
        "tri_review.github.preflight", lambda *a, **k: called.append("preflight")
    )

    result = CliRunner().invoke(main, ["--url", URL, "--pr", "999"])

    assert result.exit_code != 0
    assert called == []


# --- gating and replay ------------------------------------------------------


class _ReachedTheModels(Exception):
    """Sentinel: the run got past every gate and was about to spend money."""


def _stub_github(monkeypatch, files, complete=True, head_sha="abc123def456"):
    """Stub gh far enough to exercise the gates, and no further.

    `fetch_pr_meta` raising is the assertion mechanism: it is the first call
    after the gates, so reaching it means nothing was skipped.
    """
    monkeypatch.setattr("tri_review.github.preflight", lambda *a, **k: None)
    monkeypatch.setattr(
        "tri_review.github.fetch_changed_files", lambda *a, **k: (files, complete)
    )

    def meta(*a, **k):
        raise _ReachedTheModels()

    monkeypatch.setattr("tri_review.github.fetch_pr_meta", meta)


def test_docs_only_pr_is_skipped_without_spending_anything(monkeypatch):
    """The whole point: a docs-only PR must cost nothing and exit clean."""
    _stub_github(monkeypatch, ["README.md", "docs/guide.md", "CHANGELOG.md"])

    result = CliRunner().invoke(main, ["--repo", "octocat/Hello-World", "--pr", "42"])

    assert result.exit_code == 0
    assert "Nothing to review" in result.output


def test_skip_names_the_files_and_the_way_to_override_it(monkeypatch):
    _stub_github(monkeypatch, ["README.md"])

    result = CliRunner().invoke(main, ["--repo", "octocat/Hello-World", "--pr", "42"])

    assert "README.md" in result.output
    assert "--no-default-excludes" in result.output


def test_a_single_code_file_is_enough_to_earn_a_review(monkeypatch):
    """A mixed PR is reviewed; the docs in it are just dropped from the payload."""
    _stub_github(monkeypatch, ["README.md", "docs/guide.md", "src/auth.py"])

    result = CliRunner().invoke(main, ["--repo", "octocat/Hello-World", "--pr", "42"])

    assert isinstance(result.exception, _ReachedTheModels)


def test_no_default_excludes_reviews_the_docs_after_all(monkeypatch):
    _stub_github(monkeypatch, ["README.md"])

    result = CliRunner().invoke(
        main, ["--repo", "octocat/Hello-World", "--pr", "42", "--no-default-excludes"]
    )

    assert isinstance(result.exception, _ReachedTheModels)


def test_a_truncated_file_list_never_skips(monkeypatch):
    """Skipping on a page of files we know is partial could miss real code."""
    _stub_github(monkeypatch, ["README.md"] * 3, complete=False)

    result = CliRunner().invoke(main, ["--repo", "octocat/Hello-World", "--pr", "42"])

    assert isinstance(result.exception, _ReachedTheModels)
    assert "inconclusive" in result.output


def test_an_empty_file_list_is_left_to_the_diff_fetch(monkeypatch):
    """No files listed is not a skip decision -- it is a question for gh."""
    _stub_github(monkeypatch, [])

    result = CliRunner().invoke(main, ["--repo", "octocat/Hello-World", "--pr", "42"])

    assert isinstance(result.exception, _ReachedTheModels)


def _seed_history(tmp_path, monkeypatch, head_sha="abc123def456", models=None):
    from tri_review import config, history

    monkeypatch.setenv("TRI_REVIEW_HISTORY_DIR", str(tmp_path))
    history.save(
        history.RunRecord(
            repo="octocat/Hello-World",
            pr="42",
            head_sha=head_sha,
            models=models or [config.model_a(), config.model_b(), config.model_c()],
            excludes=list(config.default_excludes()),
            report="## Consensus Findings\n\nStored report from the last run.",
        )
    )


def test_an_unchanged_pr_replays_instead_of_paying_again(monkeypatch, tmp_path):
    monkeypatch.setattr("tri_review.github.preflight", lambda *a, **k: None)
    monkeypatch.setattr(
        "tri_review.github.fetch_changed_files", lambda *a, **k: (["src/auth.py"], True)
    )
    monkeypatch.setattr(
        "tri_review.github.fetch_pr_meta",
        lambda *a, **k: {"head_sha": "abc123def456", "url": ""},
    )
    _seed_history(tmp_path, monkeypatch)

    result = CliRunner().invoke(main, ["--repo", "octocat/Hello-World", "--pr", "42"])

    assert result.exit_code == 0
    assert "No change since the last review" in result.output
    assert "Stored report from the last run" in result.output


def test_fresh_buys_a_new_review_even_when_nothing_changed(monkeypatch, tmp_path):
    monkeypatch.setattr("tri_review.github.preflight", lambda *a, **k: None)
    monkeypatch.setattr(
        "tri_review.github.fetch_changed_files", lambda *a, **k: (["src/auth.py"], True)
    )
    monkeypatch.setattr(
        "tri_review.github.fetch_pr_meta",
        lambda *a, **k: {"head_sha": "abc123def456", "url": ""},
    )
    _seed_history(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "tri_review.graph.build_review_graph", lambda **k: (_ for _ in ()).throw(_ReachedTheModels())
    )

    result = CliRunner().invoke(
        main, ["--repo", "octocat/Hello-World", "--pr", "42", "--fresh"]
    )

    assert isinstance(result.exception, _ReachedTheModels)


def test_a_new_commit_invalidates_the_stored_review(monkeypatch, tmp_path):
    monkeypatch.setattr("tri_review.github.preflight", lambda *a, **k: None)
    monkeypatch.setattr(
        "tri_review.github.fetch_changed_files", lambda *a, **k: (["src/auth.py"], True)
    )
    monkeypatch.setattr(
        "tri_review.github.fetch_pr_meta",
        lambda *a, **k: {"head_sha": "999newsha999", "url": ""},
    )
    _seed_history(tmp_path, monkeypatch, head_sha="abc123def456")
    monkeypatch.setattr(
        "tri_review.graph.build_review_graph", lambda **k: (_ for _ in ()).throw(_ReachedTheModels())
    )

    result = CliRunner().invoke(main, ["--repo", "octocat/Hello-World", "--pr", "42"])

    assert isinstance(result.exception, _ReachedTheModels)
    assert "out of date" in result.output


# --- exclude resolution -----------------------------------------------------


def test_explicit_excludes_add_to_the_defaults_rather_than_replacing_them():
    from tri_review.cli import _resolve_excludes

    patterns = _resolve_excludes(("**/fixtures/**",), skip_defaults=False)
    assert "**/fixtures/**" in patterns
    assert "**/*.md" in patterns


def test_opting_out_of_the_defaults_keeps_only_what_was_asked_for():
    from tri_review.cli import _resolve_excludes

    assert _resolve_excludes(("**/fixtures/**",), skip_defaults=True) == ("**/fixtures/**",)


def test_duplicate_patterns_are_collapsed():
    from tri_review.cli import _resolve_excludes

    patterns = _resolve_excludes(("**/*.md",), skip_defaults=False)
    assert patterns.count("**/*.md") == 1

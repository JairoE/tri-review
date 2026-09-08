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

    patterns, _ = _resolve_excludes(("**/fixtures/**",), skip_defaults=False)
    assert "**/fixtures/**" in patterns
    assert "**/*.md" in patterns


def test_opting_out_of_the_defaults_keeps_only_what_was_asked_for():
    from tri_review.cli import _resolve_excludes

    patterns, skippable = _resolve_excludes(("**/fixtures/**",), skip_defaults=True)
    assert patterns == ("**/fixtures/**",)
    assert skippable == ("**/fixtures/**",)


def test_duplicate_patterns_are_collapsed():
    from tri_review.cli import _resolve_excludes

    patterns, _ = _resolve_excludes(("**/*.md",), skip_defaults=False)
    assert patterns.count("**/*.md") == 1


def test_lockfiles_leave_the_payload_but_do_not_justify_skipping():
    """The two roles an exclude pattern can play, kept apart."""
    from tri_review.cli import _resolve_excludes

    patterns, skippable = _resolve_excludes((), skip_defaults=False)
    assert "**/*.lock" in patterns
    assert "**/*.lock" not in skippable
    assert "**/*.md" in patterns and "**/*.md" in skippable


def test_a_users_own_pattern_counts_in_both_roles():
    """Someone naming a pattern is saying what they do not want reviewed."""
    from tri_review.cli import _resolve_excludes

    _, skippable = _resolve_excludes(("**/*.py",), skip_defaults=False)
    assert "**/*.py" in skippable


def test_a_lockfile_only_pr_is_reviewed_rather_than_skipped(monkeypatch):
    """The shape of a dependency bump. It must not pass unreviewed on its own."""
    _stub_github(monkeypatch, ["uv.lock", "package-lock.json"])

    result = CliRunner().invoke(main, ["--repo", "octocat/Hello-World", "--pr", "42"])

    assert isinstance(result.exception, _ReachedTheModels)
    # Asserted on fragments: rich hard-wraps the notice mid-phrase.
    assert "not grounds" in result.output
    assert "uv.lock" in result.output


def test_a_docs_and_lockfile_pr_still_reviews_the_lockfile(monkeypatch):
    _stub_github(monkeypatch, ["README.md", "uv.lock"])

    result = CliRunner().invoke(main, ["--repo", "octocat/Hello-World", "--pr", "42"])

    assert isinstance(result.exception, _ReachedTheModels)


def test_a_docs_only_pr_is_still_skipped(monkeypatch):
    """The relaxation must not have made the ordinary docs case reviewable."""
    _stub_github(monkeypatch, ["README.md", "docs/a.md"])

    result = CliRunner().invoke(main, ["--repo", "octocat/Hello-World", "--pr", "42"])

    assert result.exit_code == 0
    assert "Nothing to review" in result.output


def test_a_user_excluding_everything_is_taken_at_their_word(monkeypatch):
    _stub_github(monkeypatch, ["src/auth.py"])

    result = CliRunner().invoke(
        main, ["--repo", "octocat/Hello-World", "--pr", "42", "--exclude", "**/*.py"]
    )

    assert result.exit_code == 0
    assert "Nothing to review" in result.output


def test_the_skip_status_line_names_the_gate(monkeypatch):
    _stub_github(monkeypatch, ["README.md"])

    result = CliRunner().invoke(main, ["--repo", "octocat/Hello-World", "--pr", "42"])

    assert "tri-review-status: skipped:path" in result.output


def test_a_triage_skip_is_labelled_distinctly(monkeypatch, tmp_path):
    from tri_review.triage import TriageVerdict

    _stub_for_triage(
        monkeypatch,
        tmp_path,
        verdict=TriageVerdict(changes_behavior=False, reason="comments only"),
    )

    result = CliRunner().invoke(
        main, ["--repo", "octocat/Hello-World", "--pr", "42", "--triage"]
    )

    assert "tri-review-status: skipped:triage" in result.output


# --- cwd mode: the working tree must be the commit the review is keyed on -----


def _stub_cwd_mode(monkeypatch, tmp_path, tree_reason):
    from tri_review import config, history

    monkeypatch.setenv("TRI_REVIEW_HISTORY_DIR", str(tmp_path))
    monkeypatch.setattr("tri_review.github.preflight", lambda *a, **k: None)
    monkeypatch.setattr("tri_review.github.detect_pr", lambda *a, **k: "42")
    monkeypatch.setattr(
        "tri_review.github.fetch_changed_files", lambda *a, **k: (["src/auth.py"], True)
    )
    monkeypatch.setattr(
        "tri_review.github.fetch_pr_meta",
        lambda *a, **k: {
            "head_sha": "abc123def456",
            "url": "https://github.com/octocat/Hello-World/pull/42",
        },
    )
    monkeypatch.setattr("tri_review.github.working_tree_reason", lambda sha: tree_reason)
    history.save(
        history.RunRecord(
            repo="octocat/Hello-World",
            pr="42",
            head_sha="abc123def456",
            models=[config.model_a(), config.model_b(), config.model_c()],
            excludes=list(config.default_excludes()),
            report="## Consensus Findings\n\nStored report from the last run.",
        )
    )
    monkeypatch.setattr(
        "tri_review.graph.build_review_graph",
        lambda **k: (_ for _ in ()).throw(_ReachedTheModels()),
    )


def test_a_matching_checkout_replays_in_cwd_mode(monkeypatch, tmp_path):
    _stub_cwd_mode(monkeypatch, tmp_path, tree_reason=None)

    result = CliRunner().invoke(main, ["--pr", "42"])

    assert result.exit_code == 0
    assert "Stored report from the last run" in result.output


def test_a_wrong_checkout_never_replays_a_stored_review(monkeypatch, tmp_path):
    """Reviewing from the wrong branch must not be made durable by the cache."""
    _stub_cwd_mode(
        monkeypatch, tmp_path, tree_reason="the checkout is at 999fffff, not the PR head abc123de"
    )

    result = CliRunner().invoke(main, ["--pr", "42"])

    assert isinstance(result.exception, _ReachedTheModels)
    assert "not the ones it was written about" in result.output


def test_repo_mode_does_not_consult_the_working_tree(monkeypatch, tmp_path):
    """--repo reads bodies at the SHA itself, so there is nothing to disagree with."""
    called = []
    _stub_cwd_mode(monkeypatch, tmp_path, tree_reason=None)
    monkeypatch.setattr(
        "tri_review.github.working_tree_reason", lambda sha: called.append(sha) or None
    )

    CliRunner().invoke(main, ["--repo", "octocat/Hello-World", "--pr", "42"])

    assert called == []


# --- the triage gate --------------------------------------------------------


def _stub_for_triage(monkeypatch, tmp_path, verdict, diff="diff --git a/x b/x"):
    """Get a run as far as the triage gate, with everything before it satisfied."""
    monkeypatch.setenv("TRI_REVIEW_HISTORY_DIR", str(tmp_path))
    monkeypatch.setattr("tri_review.github.preflight", lambda *a, **k: None)
    monkeypatch.setattr(
        "tri_review.github.fetch_changed_files", lambda *a, **k: (["deploy.sh"], True)
    )
    monkeypatch.setattr(
        "tri_review.github.fetch_pr_meta",
        lambda *a, **k: {"head_sha": "abc123def456", "url": ""},
    )
    monkeypatch.setattr("tri_review.github.fetch_diff", lambda *a, **k: diff)
    monkeypatch.setattr("tri_review.triage.assess", lambda *a, **k: verdict)
    monkeypatch.setattr(
        "tri_review.graph.build_review_graph",
        lambda **k: (_ for _ in ()).throw(_ReachedTheModels()),
    )


def test_triage_is_off_unless_asked_for(monkeypatch, tmp_path):
    """It costs money and can be wrong, so it is never the silent default."""
    called = []
    _stub_for_triage(monkeypatch, tmp_path, verdict=None)
    monkeypatch.setattr("tri_review.triage.assess", lambda *a, **k: called.append(1))

    result = CliRunner().invoke(main, ["--repo", "octocat/Hello-World", "--pr", "42"])

    assert isinstance(result.exception, _ReachedTheModels)
    assert called == []


def test_a_comment_only_change_is_skipped_when_triage_is_on(monkeypatch, tmp_path):
    from tri_review.triage import TriageVerdict

    _stub_for_triage(
        monkeypatch,
        tmp_path,
        verdict=TriageVerdict(changes_behavior=False, reason="only a docstring changed"),
    )

    result = CliRunner().invoke(
        main, ["--repo", "octocat/Hello-World", "--pr", "42", "--triage"]
    )

    assert result.exit_code == 0
    assert "only a docstring changed" in result.output


def test_a_triage_skip_says_it_is_a_judgement_call(monkeypatch, tmp_path):
    """A model's guess must not be presented with the authority of a path rule."""
    from tri_review.triage import TriageVerdict

    _stub_for_triage(
        monkeypatch,
        tmp_path,
        verdict=TriageVerdict(changes_behavior=False, reason="comments only"),
    )

    result = CliRunner().invoke(
        main, ["--repo", "octocat/Hello-World", "--pr", "42", "--triage"]
    )

    assert "can be wrong" in result.output
    assert "--no-triage" in result.output


def test_a_behaviour_change_verdict_proceeds_to_the_review(monkeypatch, tmp_path):
    from tri_review.triage import TriageVerdict

    _stub_for_triage(
        monkeypatch,
        tmp_path,
        verdict=TriageVerdict(changes_behavior=True, reason="control flow changed"),
    )

    result = CliRunner().invoke(
        main, ["--repo", "octocat/Hello-World", "--pr", "42", "--triage"]
    )

    assert isinstance(result.exception, _ReachedTheModels)


def test_an_inconclusive_triage_reviews_anyway(monkeypatch, tmp_path):
    """No verdict and "yes review it" must land in the same place."""
    _stub_for_triage(monkeypatch, tmp_path, verdict=None)

    result = CliRunner().invoke(
        main, ["--repo", "octocat/Hello-World", "--pr", "42", "--triage"]
    )

    assert isinstance(result.exception, _ReachedTheModels)
    assert "could not reach a verdict" in result.output.lower()


def test_triage_can_be_switched_on_by_environment(monkeypatch, tmp_path):
    from tri_review.triage import TriageVerdict

    _stub_for_triage(
        monkeypatch,
        tmp_path,
        verdict=TriageVerdict(changes_behavior=False, reason="formatting only"),
    )
    monkeypatch.setenv("TRI_REVIEW_TRIAGE", "1")

    result = CliRunner().invoke(main, ["--repo", "octocat/Hello-World", "--pr", "42"])

    assert result.exit_code == 0
    assert "formatting only" in result.output


def test_no_triage_beats_the_environment_switch(monkeypatch, tmp_path):
    from tri_review.triage import TriageVerdict

    _stub_for_triage(
        monkeypatch,
        tmp_path,
        verdict=TriageVerdict(changes_behavior=False, reason="formatting only"),
    )
    monkeypatch.setenv("TRI_REVIEW_TRIAGE", "1")

    result = CliRunner().invoke(
        main, ["--repo", "octocat/Hello-World", "--pr", "42", "--no-triage"]
    )

    assert isinstance(result.exception, _ReachedTheModels)


def test_fresh_bypasses_the_reviewer_cache_too(monkeypatch, tmp_path):
    """--fresh means "buy a new review", not "skip only the stored report"."""
    seen = {}

    def fake_build(**kwargs):
        seen.update(kwargs)
        raise _ReachedTheModels()

    _stub_for_triage(monkeypatch, tmp_path, verdict=None)
    monkeypatch.setattr("tri_review.graph.build_review_graph", fake_build)

    CliRunner().invoke(main, ["--repo", "octocat/Hello-World", "--pr", "42", "--fresh"])

    assert seen["use_cache"] is False


def test_a_normal_run_keeps_the_reviewer_cache_on(monkeypatch, tmp_path):
    seen = {}

    def fake_build(**kwargs):
        seen.update(kwargs)
        raise _ReachedTheModels()

    _stub_for_triage(monkeypatch, tmp_path, verdict=None)
    monkeypatch.setattr("tri_review.graph.build_review_graph", fake_build)

    CliRunner().invoke(main, ["--repo", "octocat/Hello-World", "--pr", "42"])

    assert seen["use_cache"] is True


# --- adversarial review: the gates must not hand a skip to the diff fetch ----


def test_a_truncated_file_list_keeps_lockfiles_in_the_payload(monkeypatch):
    """The inconclusive branch used to return the payload exclude set.

    On a lockfile-only PR whose file list came back short, those patterns strip
    every changed file, `gh pr diff` returns nothing, and the run exits 0 as
    `skipped:empty-diff` -- immediately after printing "Reviewing." The branch
    that admits it cannot see the whole PR must be the most conservative one,
    not the one that applies the widest exclude set.
    """
    from tri_review import cli as cli_module
    from tri_review.config import default_excludes, skip_eligible_excludes

    monkeypatch.setattr(
        "tri_review.github.fetch_changed_files",
        lambda *a, **k: (["uv.lock", "package-lock.json"], False),
    )

    _, effective = cli_module._gate_paths(
        "42", "octocat/Hello-World", default_excludes(), skip_eligible_excludes()
    )

    assert "**/*.lock" not in effective
    assert "package-lock.json" not in effective


def test_the_skip_remedy_matches_who_did_the_excluding(monkeypatch):
    """--no-default-excludes cannot lift a pattern the user typed themselves."""
    _stub_github(monkeypatch, ["src/auth.py"])

    result = CliRunner().invoke(
        main,
        ["--repo", "octocat/Hello-World", "--pr", "42", "--exclude", "src/**"],
    )

    assert result.exit_code == 0
    assert "--no-default-excludes" not in result.output
    assert "patterns you supplied" in result.output


def test_code_under_docs_is_not_grounds_for_skipping(monkeypatch):
    """`docs/**` was skip-eligible, so a Sphinx conf.py skipped at exit 0."""
    _stub_github(monkeypatch, ["docs/conf.py", "docs/scripts/publish.sh"])

    result = CliRunner().invoke(main, ["--repo", "octocat/Hello-World", "--pr", "42"])

    assert isinstance(result.exception, _ReachedTheModels)


def test_a_source_file_named_after_the_changelog_is_still_source(monkeypatch):
    """`**/CHANGELOG*` matched any basename starting with the word."""
    _stub_github(monkeypatch, ["tools/CHANGELOG_generator.py"])

    result = CliRunner().invoke(main, ["--repo", "octocat/Hello-World", "--pr", "42"])

    assert isinstance(result.exception, _ReachedTheModels)

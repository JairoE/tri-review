"""The Action's comment step, driven against a stub `gh`.

This is the only part of tri-review that mutates something outside the runner,
and until it was extracted from action.yml it could not be run at all without
pushing a commit. The stub records every `gh` invocation in order, so these
tests assert on the actual sequence of API calls -- which matters here, because
the ordering (post the new report before hiding the old one) is the difference
between a transient double-report and a PR with no visible review on it.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "pr_comment.sh"
MARKER = "<!-- tri-review-report -->"
SUPERSEDED_MARKER = "<!-- tri-review-superseded -->"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="the comment script shells out to jq"
)

# The stub is a Python script rather than more bash: it needs to base64-encode
# comment bodies the same way the real `gh api ... --jq '... | @base64'` would,
# introspect which ids a given nodes(ids:) call actually carried (to answer a
# batched call correctly), and remember which target has already had a
# simulated transient failure -- none of which is worth doing in shell.
_STUB = '''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
joined = " ".join(argv)
stdin = ""
if "--input" in argv:
    stdin = sys.stdin.read()
with open(os.environ["GH_LOG"], "a") as log:
    log.write(json.dumps({"argv": argv, "stdin": stdin}) + "\\n")

def fixture(name, default=""):
    return os.environ.get(name, default)

if argv[:2] == ["api", "user"]:
    sys.exit(1)  # the default GITHUB_TOKEN is a bot, not a user

if "graphql" in argv:
    if "minimizeComment" in joined:
        target = next(
            (a.split("=", 1)[1] for a in argv if a.startswith("id=")), None
        )
        if fixture("STUB_MINIMIZE_FAILS", "0") == "1":
            sys.exit(1)
        fail_once = set(fixture("STUB_MINIMIZE_FAIL_ONCE").split())
        if target in fail_once:
            marker = os.path.join(
                os.environ.get("STUB_STATE_DIR", "."), f"tried-{target}"
            )
            if not os.path.exists(marker):
                open(marker, "w").close()
                sys.exit(1)  # first attempt: a transient failure
        sys.exit(0)  # retry (or an id nobody asked to fail) succeeds
    # nodes(ids:) lookup. Answer only for the ids THIS call actually carried,
    # so a caller that batches into multiple invocations gets a distinct,
    # correct answer each time -- proof the script is really chunking rather
    # than sending one oversized request.
    call_ids = [a.split("ids[]=", 1)[1] for a in argv if a.startswith("ids[]=")]
    not_minimized = set(fixture("STUB_NOT_MINIMIZED").split())
    for node_id in call_ids:
        if node_id in not_minimized:
            print(node_id)
    sys.exit(0)

if "--paginate" in argv:
    # STUB_COMMENTS lines are already `id<TAB>node_id<TAB>base64(body)` -- the
    # test helper (_comment) encodes the body itself, the same as jq's `@base64`
    # would in the real call, so no per-line body ever contains a raw newline
    # that a naive splitlines() here could mistake for a record boundary.
    print(fixture("STUB_COMMENTS"), end="")
    sys.exit(0)

sys.exit(0)
'''


def _run(tmp_path, *, mode="append", comments="", **env_extra):
    """`comments` is `id<TAB>node_id<TAB>plain body` lines, oldest first --
    the same shape prior_comments() extracts, before this stub base64-encodes
    the body column the way the real `gh api --jq` call would.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "gh"
    stub.write_text(_STUB)
    stub.chmod(0o755)

    body = tmp_path / "comment-body.md"
    body.write_text(f"{MARKER}\n\nthe new report\n")
    log = tmp_path / "gh.log"

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GH_LOG": str(log),
        "GH_TOKEN": "stub",
        "REPO": "octocat/Hello-World",
        "PR_NUMBER": "42",
        "BODY_FILE": str(body),
        "MARKER": MARKER,
        "MODE": mode,
        "COMMIT_SHA": "abc1234",
        "RUN_URL": "https://example.invalid/run/1",
        "STUB_COMMENTS": comments,
        "STUB_STATE_DIR": str(tmp_path),
        **env_extra,
    }
    result = subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return result, calls


def _kinds(calls):
    """Reduce each recorded gh invocation to a short label, in order."""
    kinds = []
    for call in calls:
        joined = " ".join(call["argv"])
        if call["argv"][:2] == ["api", "user"]:
            kinds.append("whoami")
        elif "--paginate" in call["argv"]:
            kinds.append("list")
        elif "minimizeComment" in joined:
            kinds.append("minimize")
        elif "graphql" in call["argv"]:
            kinds.append("is-minimized")
        elif "-X POST" in joined:
            kinds.append("post")
        elif "-X PATCH" in joined:
            kinds.append("patch")
        else:
            kinds.append(joined)
    return kinds


def _comment(rest_id, node_id, body=""):
    """Build one `prior_comments()` row: `id<TAB>node_id<TAB>base64(body)`.

    The body is base64-encoded here, on the test side, rather than left as
    plain text for the stub to encode -- a real Markdown report body contains
    embedded newlines, and STUB_COMMENTS packs multiple rows together separated
    by "\n". Encoding first is what keeps a body's own newlines from being
    mistaken for row boundaries, exactly as jq's `@base64` does in production.
    """
    plain = body or f"{MARKER}\n\nold report"
    encoded = base64.b64encode(plain.encode()).decode()
    return f"{rest_id}\t{node_id}\t{encoded}"


def test_append_posts_a_new_comment_and_hides_the_previous_one(tmp_path):
    result, calls = _run(
        tmp_path,
        comments=_comment(101, "NODE_1"),
        STUB_NOT_MINIMIZED="NODE_1",
    )

    assert result.returncode == 0, result.stderr
    assert _kinds(calls) == ["whoami", "list", "post", "is-minimized", "minimize"]


def test_the_new_report_is_posted_before_the_old_one_is_hidden(tmp_path):
    """If hiding is what fails, the PR keeps two visible reports. If posting
    failed after a hide, it would keep none."""
    _, calls = _run(
        tmp_path, comments=_comment(101, "NODE_1"), STUB_NOT_MINIMIZED="NODE_1"
    )

    kinds = _kinds(calls)
    assert kinds.index("post") < kinds.index("minimize")


def test_the_first_run_on_a_pr_has_nothing_to_hide(tmp_path):
    result, calls = _run(tmp_path, comments="")

    assert result.returncode == 0, result.stderr
    assert _kinds(calls) == ["whoami", "list", "post"]


def test_every_earlier_report_is_hidden_not_just_the_last(tmp_path):
    _, calls = _run(
        tmp_path,
        comments="\n".join(
            [_comment(101, "NODE_1"), _comment(102, "NODE_2"), _comment(103, "NODE_3")]
        ),
        STUB_NOT_MINIMIZED="NODE_1 NODE_2 NODE_3",
    )

    assert _kinds(calls).count("minimize") == 3


def test_comments_github_already_considers_hidden_are_left_alone(tmp_path):
    """Otherwise every run re-hides every previous run's comment: one mutation
    per comment per commit, growing with the length of the PR."""
    _, calls = _run(
        tmp_path,
        comments="\n".join(
            [_comment(101, "NODE_1"), _comment(102, "NODE_2"), _comment(103, "NODE_3")]
        ),
        STUB_NOT_MINIMIZED="NODE_3",
    )

    assert _kinds(calls).count("minimize") == 1
    minimize = next(c for c in calls if "minimizeComment" in " ".join(c["argv"]))
    assert "NODE_3" in " ".join(minimize["argv"])


def test_a_report_already_folded_is_skipped_without_any_api_call(tmp_path):
    """A comment already collapsed into a <details> block is a one-way trip --
    it must not cost a GraphQL retry or a body fetch on every future run."""
    folded_body = f"{MARKER}\n{SUPERSEDED_MARKER}\n\nalready folded"
    _, calls = _run(
        tmp_path, comments=_comment(101, "NODE_1", folded_body), STUB_NOT_MINIMIZED="NODE_1"
    )

    kinds = _kinds(calls)
    assert kinds == ["whoami", "list", "post"]
    assert "minimize" not in kinds and "is-minimized" not in kinds


def test_a_token_that_cannot_hide_falls_back_to_folding_the_old_report(tmp_path):
    """Minimizing needs more than the `pull-requests: write` that posting needs.
    A token without it must still not leave two reports looking equally current."""
    result, calls = _run(
        tmp_path,
        comments=_comment(101, "NODE_1"),
        STUB_NOT_MINIMIZED="NODE_1",
        STUB_MINIMIZE_FAILS="1",
    )

    assert result.returncode == 0, result.stderr
    assert _kinds(calls) == [
        "whoami",
        "list",
        "post",
        "is-minimized",
        "minimize",
        "minimize",
        "patch",
    ]
    patch = next(c for c in calls if "-X PATCH" in " ".join(c["argv"]))
    body = json.loads(patch["stdin"])["body"]
    assert SUPERSEDED_MARKER in body
    assert "<details>" in body and "abc1234" in body
    assert "old report" in body  # the earlier report is folded, never discarded


def test_a_transient_minimize_failure_is_retried_before_falling_back(tmp_path):
    """A rate limit or a momentary API hiccup is not the same fact as the token
    lacking `issues: write`, and folding is a one-way trip -- a transient
    failure must not spend it."""
    result, calls = _run(
        tmp_path,
        comments=_comment(101, "NODE_1"),
        STUB_NOT_MINIMIZED="NODE_1",
        STUB_MINIMIZE_FAIL_ONCE="NODE_1",
    )

    assert result.returncode == 0, result.stderr
    assert _kinds(calls) == ["whoami", "list", "post", "is-minimized", "minimize", "minimize"]
    assert "patch" not in _kinds(calls)  # the retry succeeded; no fallback needed


def test_the_node_lookup_is_batched_at_one_hundred_ids(tmp_path):
    """GitHub's node(ids:) lookup accepts at most 100 ids per call. A PR with a
    longer review history than that must be batched, not truncated."""
    comments = "\n".join(_comment(100 + i, f"NODE_{i}") for i in range(150))
    not_minimized = " ".join(f"NODE_{i}" for i in range(150))

    result, calls = _run(tmp_path, comments=comments, STUB_NOT_MINIMIZED=not_minimized)

    assert result.returncode == 0, result.stderr
    lookups = [c for c in calls if "graphql" in c["argv"] and "minimizeComment" not in " ".join(c["argv"])]
    assert len(lookups) == 2  # 150 ids -> a 100-id batch and a 50-id batch
    sizes = [sum(1 for a in c["argv"] if a.startswith("ids[]=")) for c in lookups]
    assert sorted(sizes) == [50, 100]
    assert _kinds(calls).count("minimize") == 150


def test_every_earlier_report_still_gets_hidden_across_batches(tmp_path):
    comments = "\n".join(_comment(100 + i, f"NODE_{i}") for i in range(120))
    not_minimized = " ".join(f"NODE_{i}" for i in range(120))

    _, calls = _run(tmp_path, comments=comments, STUB_NOT_MINIMIZED=not_minimized)

    minimized_targets = {
        next(a.split("=", 1)[1] for a in c["argv"] if a.startswith("id="))
        for c in calls
        if "minimizeComment" in " ".join(c["argv"])
    }
    assert minimized_targets == {f"NODE_{i}" for i in range(120)}


def test_update_mode_edits_the_newest_comment_not_the_oldest(tmp_path):
    """Prior comments are returned oldest-first. Editing ids[0] here landed on
    the oldest (and, once append-mode history exists, usually already-hidden)
    comment forever, instead of the current report."""
    result, calls = _run(
        tmp_path,
        mode="update",
        comments="\n".join([_comment(101, "NODE_1"), _comment(102, "NODE_2")]),
    )

    assert result.returncode == 0, result.stderr
    patch = next(c for c in calls if "-X PATCH" in " ".join(c["argv"]))
    assert "issues/comments/102" in " ".join(patch["argv"])


def test_update_mode_reconciles_leftover_append_mode_history(tmp_path):
    """Switching comment-mode from append to update should not leave every
    earlier append-mode report sitting there unminimized."""
    result, calls = _run(
        tmp_path,
        mode="update",
        comments="\n".join(
            [_comment(101, "NODE_1"), _comment(102, "NODE_2"), _comment(103, "NODE_3")]
        ),
        STUB_NOT_MINIMIZED="NODE_1 NODE_2",
    )

    assert result.returncode == 0, result.stderr
    kinds = _kinds(calls)
    assert kinds.count("patch") == 1  # only the newest comment is edited
    assert kinds.count("minimize") == 2  # the two older ones are reconciled
    patch = next(c for c in calls if "-X PATCH" in " ".join(c["argv"]))
    assert "issues/comments/103" in " ".join(patch["argv"])


def test_update_mode_posts_when_there_is_nothing_to_edit(tmp_path):
    _, calls = _run(tmp_path, mode="update", comments="")

    assert _kinds(calls) == ["whoami", "list", "post"]


def test_the_posted_body_is_sent_as_json_not_interpolated(tmp_path):
    """A report full of backticks, quotes and newlines must not be able to break
    the request -- `gh api -f key=@file` does not read files, only `--input` does."""
    _, calls = _run(tmp_path, comments="")

    post = next(c for c in calls if "-X POST" in " ".join(c["argv"]))
    assert json.loads(post["stdin"])["body"].startswith(MARKER)

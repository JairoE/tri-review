"""The Action's comment step, driven against a stub `gh`.

This is the only part of tri-review that mutates something outside the runner,
and until it was extracted from action.yml it could not be run at all without
pushing a commit. The stub records every `gh` invocation in order, so these
tests assert on the actual sequence of API calls -- which matters here, because
the ordering (post the new report before hiding the old one) is the difference
between a transient double-report and a PR with no visible review on it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "pr_comment.sh"
MARKER = "<!-- tri-review-report -->"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="the comment script shells out to jq"
)

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
        sys.exit(int(fixture("STUB_MINIMIZE_FAILS", "0")))
    for node_id in fixture("STUB_NOT_MINIMIZED").split():
        print(node_id)
    sys.exit(0)
if "--paginate" in argv:
    print(fixture("STUB_COMMENTS"), end="")
    sys.exit(0)
if joined.endswith(".body"):
    print(fixture("STUB_BODY", "MARKER\\n\\nold report"))
    sys.exit(0)
sys.exit(0)
'''


def _run(tmp_path, *, mode="append", comments="", not_minimized="", **env_extra):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "gh"
    stub.write_text(_STUB.replace("MARKER", MARKER))
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
        "STUB_NOT_MINIMIZED": not_minimized,
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
        elif joined.endswith(".body"):
            kinds.append("read-body")
        else:
            kinds.append(joined)
    return kinds


def test_append_posts_a_new_comment_and_hides_the_previous_one(tmp_path):
    result, calls = _run(
        tmp_path, comments="101\tNODE_1\n", not_minimized="NODE_1"
    )

    assert result.returncode == 0, result.stderr
    assert _kinds(calls) == ["whoami", "list", "post", "is-minimized", "minimize"]


def test_the_new_report_is_posted_before_the_old_one_is_hidden(tmp_path):
    """If hiding is what fails, the PR keeps two visible reports. If posting
    failed after a hide, it would keep none."""
    _, calls = _run(tmp_path, comments="101\tNODE_1\n", not_minimized="NODE_1")

    kinds = _kinds(calls)
    assert kinds.index("post") < kinds.index("minimize")


def test_the_first_run_on_a_pr_has_nothing_to_hide(tmp_path):
    result, calls = _run(tmp_path, comments="")

    assert result.returncode == 0, result.stderr
    assert _kinds(calls) == ["whoami", "list", "post"]
    assert "minimize" not in _kinds(calls)


def test_every_earlier_report_is_hidden_not_just_the_last(tmp_path):
    _, calls = _run(
        tmp_path,
        comments="101\tNODE_1\n102\tNODE_2\n103\tNODE_3\n",
        not_minimized="NODE_1 NODE_2 NODE_3",
    )

    assert _kinds(calls).count("minimize") == 3


def test_comments_github_already_considers_hidden_are_left_alone(tmp_path):
    """Otherwise every run re-hides every previous run's comment: one mutation
    per comment per commit, growing with the length of the PR."""
    _, calls = _run(
        tmp_path,
        comments="101\tNODE_1\n102\tNODE_2\n103\tNODE_3\n",
        not_minimized="NODE_3",
    )

    assert _kinds(calls).count("minimize") == 1
    minimize = next(c for c in calls if "minimizeComment" in " ".join(c["argv"]))
    assert "NODE_3" in " ".join(minimize["argv"])


def test_a_token_that_cannot_hide_falls_back_to_folding_the_old_report(tmp_path):
    """Minimizing needs more than the `pull-requests: write` that posting needs.
    A token without it must still not leave two reports looking equally current."""
    result, calls = _run(
        tmp_path,
        comments="101\tNODE_1\n",
        not_minimized="NODE_1",
        STUB_MINIMIZE_FAILS="1",
    )

    assert result.returncode == 0, result.stderr
    assert _kinds(calls) == [
        "whoami",
        "list",
        "post",
        "is-minimized",
        "minimize",
        "read-body",
        "patch",
    ]
    patch = next(c for c in calls if "-X PATCH" in " ".join(c["argv"]))
    body = json.loads(patch["stdin"])["body"]
    assert "<!-- tri-review-superseded -->" in body
    assert "<details>" in body and "abc1234" in body
    assert "old report" in body  # the earlier report is folded, never discarded


def test_a_report_already_folded_is_not_folded_again(tmp_path):
    _, calls = _run(
        tmp_path,
        comments="101\tNODE_1\n",
        not_minimized="NODE_1",
        STUB_MINIMIZE_FAILS="1",
        STUB_BODY=f"{MARKER}\n<!-- tri-review-superseded -->\n\nalready folded",
    )

    assert "patch" not in _kinds(calls)


def test_update_mode_still_edits_one_comment_in_place(tmp_path):
    """The old behaviour stays available for PRs where a comment per commit is
    more noise than history."""
    result, calls = _run(tmp_path, mode="update", comments="101\tNODE_1\n")

    assert result.returncode == 0, result.stderr
    assert _kinds(calls) == ["whoami", "list", "patch"]
    patch = next(c for c in calls if "-X PATCH" in " ".join(c["argv"]))
    assert "issues/comments/101" in " ".join(patch["argv"])


def test_update_mode_posts_when_there_is_nothing_to_edit(tmp_path):
    _, calls = _run(tmp_path, mode="update", comments="")

    assert _kinds(calls) == ["whoami", "list", "post"]


def test_the_posted_body_is_sent_as_json_not_interpolated(tmp_path):
    """A report full of backticks, quotes and newlines must not be able to break
    the request -- `gh api -f key=@file` does not read files, only `--input` does."""
    _, calls = _run(tmp_path, comments="")

    post = next(c for c in calls if "-X POST" in " ".join(c["argv"]))
    assert json.loads(post["stdin"])["body"].startswith(MARKER)

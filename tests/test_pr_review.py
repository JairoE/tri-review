"""The Action's inline-suggestions step, driven against a stub `gh` (R9, AC5).

`scripts/pr_review.sh` posts the review payload `tri-review --review-output`
wrote. Like `pr_comment.sh` it mutates the PR, so these tests record every `gh`
call and assert on what was actually sent. The stub keeps state across runs in
`tmp_path`: a review it accepts is listed back on the next run as comments by
the bot, which is what lets a second run with the same payload prove it posts
nothing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "pr_review.sh"
BOT = "github-actions[bot]"
HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
FP_A = "aaaaaaaaaaaaaaaa"
FP_B = "bbbbbbbbbbbbbbbb"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="the review script shells out to jq"
)

_STUB = '''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
stdin = sys.stdin.read() if "--input" in argv else ""
with open(os.environ["GH_LOG"], "a") as log:
    log.write(json.dumps({"argv": argv, "stdin": stdin}) + "\\n")

state = os.path.join(os.environ["STUB_STATE_DIR"], "posted.json")

def posted():
    if not os.path.exists(state):
        return []
    with open(state) as f:
        return json.load(f)

def remember(comments):
    everything = posted() + comments
    with open(state, "w") as f:
        json.dump(everything, f)

def fail(status, message):
    print(json.dumps({"message": message, "status": str(status)}))
    print(f"gh: {message} (HTTP {status})", file=sys.stderr)
    sys.exit(1)

if argv[:2] == ["api", "user"]:
    sys.exit(1)  # the default GITHUB_TOKEN is a bot, not a user

if "--paginate" in argv:
    if os.environ.get("STUB_LIST_FAILS") == "1":
        fail(502, "Bad Gateway")
    prior = json.loads(os.environ.get("STUB_PRIOR", "[]"))
    # Two pages, the way `gh api --paginate` prints them: arrays back to back.
    print(json.dumps(prior))
    print(json.dumps(posted()))
    sys.exit(0)

endpoint = argv[1]
if endpoint.endswith("/reviews"):
    status = os.environ.get("STUB_REVIEW_STATUS", "")
    if status:
        fail(int(status), "Unprocessable Entity" if status == "422" else "Resource not accessible by integration")
    payload = json.loads(stdin)
    remember([{"user": {"login": "%s"}, "body": c["body"]} for c in payload["comments"]])
    print(json.dumps({"id": 1}))
    sys.exit(0)

if endpoint.endswith("/comments"):
    comment = json.loads(stdin)
    if comment["path"] in os.environ.get("STUB_FAIL_PATHS", "").split():
        fail(422, "Validation Failed")
    remember([{"user": {"login": "%s"}, "body": comment["body"]}])
    print(json.dumps({"id": 2}))
    sys.exit(0)

sys.exit(0)
''' % (BOT, BOT)


def _comment(fp, path, line, start_line=None):
    body = (
        f"<!-- tri-review-suggestion:{fp} -->\n"
        f"### F1 · **major** · bug — title for {path}\n\n"
        "```suggestion\nfixed = True\n```"
    )
    c = {"path": path}
    if start_line is not None:
        c |= {"start_line": start_line, "start_side": "RIGHT"}
    return c | {"line": line, "side": "RIGHT", "body": body}


def _payload():
    return {
        "commit_id": HEAD_SHA,
        "event": "COMMENT",
        "body": "<!-- tri-review-suggestions -->\n**tri-review suggested changes** for `0123456`: 2 fixes from the report.",
        "comments": [
            _comment(FP_A, "src/a.py", 12, start_line=10),
            _comment(FP_B, "src/b.py", 7),
        ],
    }


def _prior(login, fp):
    return {"user": {"login": login}, "body": f"<!-- tri-review-suggestion:{fp} -->\nold"}


def _run(tmp_path, payload=None, **env_extra):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "gh"
    stub.write_text(_STUB)
    stub.chmod(0o755)

    review = tmp_path / "review.json"
    review.write_text(json.dumps(payload or _payload()))
    log = tmp_path / "gh.log"
    log.unlink(missing_ok=True)
    out = tmp_path / "github_output"
    out.write_text("")

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GH_LOG": str(log),
        "GH_TOKEN": "stub",
        "REPO": "octocat/Hello-World",
        "PR_NUMBER": "42",
        "REVIEW_FILE": str(review),
        "GITHUB_OUTPUT": str(out),
        "STUB_STATE_DIR": str(tmp_path),
        **env_extra,
    }
    result = subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return result, calls, out.read_text()


def _posts(calls, suffix):
    return [
        json.loads(c["stdin"])
        for c in calls
        if "--input" in c["argv"] and c["argv"][1].endswith(suffix)
    ]


def test_posts_one_review_with_all_comments(tmp_path):
    result, calls, _ = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    reviews = _posts(calls, "/pulls/42/reviews")
    assert reviews == [_payload()]
    assert _posts(calls, "/pulls/42/comments") == []
    listing = next(c for c in calls if "--paginate" in c["argv"])
    assert listing["argv"][1] == "repos/octocat/Hello-World/pulls/42/comments"


def test_already_posted_fingerprints_are_skipped(tmp_path):
    result, calls, _ = _run(tmp_path, STUB_PRIOR=json.dumps([_prior(BOT, FP_A)]))

    assert result.returncode == 0, result.stderr
    (review,) = _posts(calls, "/pulls/42/reviews")
    assert [c["path"] for c in review["comments"]] == ["src/b.py"]
    assert review["commit_id"] == HEAD_SHA


def test_nothing_left_posts_nothing_and_says_so(tmp_path):
    """AC5: a second run with the same payload posts nothing."""
    first, first_calls, _ = _run(tmp_path)
    assert first.returncode == 0, first.stderr
    assert len(_posts(first_calls, "/reviews")) == 1

    second, calls, output = _run(tmp_path)

    assert second.returncode == 0, second.stderr
    assert _posts(calls, "/reviews") == []
    assert _posts(calls, "/comments") == []
    assert "All suggestions already posted." in second.stdout
    assert "suggestions-posted=0" in output


def test_fingerprints_from_other_users_are_ignored(tmp_path):
    """The spoofing guard: anyone can comment a fingerprint marker on the PR,
    and that must not suppress the bot's own suggestion."""
    prior = [_prior("mallory", FP_A), _prior("mallory", FP_B)]
    result, calls, _ = _run(tmp_path, STUB_PRIOR=json.dumps(prior))

    assert result.returncode == 0, result.stderr
    (review,) = _posts(calls, "/reviews")
    assert len(review["comments"]) == 2


def test_422_falls_back_to_per_comment_posts_with_commit_id(tmp_path):
    result, calls, output = _run(tmp_path, STUB_REVIEW_STATUS="422")

    assert result.returncode == 0, result.stderr
    singles = _posts(calls, "/pulls/42/comments")
    assert [c["path"] for c in singles] == ["src/a.py", "src/b.py"]
    assert all(c["commit_id"] == HEAD_SHA for c in singles)
    assert singles[0]["start_line"] == 10 and singles[0]["line"] == 12
    assert singles[0]["body"] == _payload()["comments"][0]["body"]
    assert "suggestions-posted=2" in output


def test_one_failed_fallback_comment_warns_and_continues(tmp_path):
    result, calls, output = _run(
        tmp_path, STUB_REVIEW_STATUS="422", STUB_FAIL_PATHS="src/a.py"
    )

    assert result.returncode == 0, result.stderr
    assert [c["path"] for c in _posts(calls, "/pulls/42/comments")] == ["src/a.py", "src/b.py"]
    warnings = [line for line in result.stdout.splitlines() if line.startswith("::warning::")]
    assert any("src/a.py" in w for w in warnings)
    assert not any("src/b.py" in w for w in warnings)
    assert "suggestions-posted=1" in output


def test_403_warns_and_exits_zero(tmp_path):
    result, calls, output = _run(tmp_path, STUB_REVIEW_STATUS="403")

    assert result.returncode == 0, result.stderr
    assert any(line.startswith("::warning::") for line in result.stdout.splitlines())
    assert _posts(calls, "/pulls/42/comments") == []
    assert "suggestions-posted=0" in output


def test_writes_suggestions_posted_output(tmp_path):
    result, _, output = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    assert output.splitlines() == ["suggestions-posted=2"]

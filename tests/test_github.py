import base64
import json
import subprocess

import pytest

from tri_review import github
from tri_review.errors import PreflightError, PRNotFoundError


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _recorder(monkeypatch, result=None, results=None):
    """Capture every argv `_run` is handed, returning a canned result.

    Returned list stays empty when no subprocess was launched, which is how the
    "refused before requesting" assertions prove no request went out.
    """
    calls: list[list[str]] = []

    def fake_run(args):
        calls.append(args)
        if results is not None:
            return results(args)
        return result if result is not None else _proc()

    monkeypatch.setattr(github, "_run", fake_run)
    return calls


def test_preflight_missing_gh(monkeypatch):
    monkeypatch.setattr(github.shutil, "which", lambda _: None)
    with pytest.raises(PreflightError, match="not found on PATH"):
        github.preflight()


def test_preflight_not_a_repo(monkeypatch):
    monkeypatch.setattr(github.shutil, "which", lambda _: "/usr/local/bin/gh")
    monkeypatch.setattr(github, "_run", lambda args: _proc(returncode=128))
    with pytest.raises(PreflightError, match="Not inside a git repository"):
        github.preflight()


def test_preflight_unauthenticated(monkeypatch):
    monkeypatch.setattr(github.shutil, "which", lambda _: "/usr/local/bin/gh")

    def fake_run(args):
        if args[0] == "git":
            return _proc(stdout=".git")
        if args[:3] == ["gh", "auth", "status"]:
            return _proc(returncode=1, stderr="not logged in")
        return _proc()

    monkeypatch.setattr(github, "_run", fake_run)
    with pytest.raises(PreflightError, match="not authenticated"):
        github.preflight()


def test_preflight_no_github_remote(monkeypatch):
    monkeypatch.setattr(github.shutil, "which", lambda _: "/usr/local/bin/gh")

    def fake_run(args):
        if args[:3] == ["gh", "repo", "view"]:
            return _proc(returncode=1, stderr="no remote")
        return _proc(stdout=".git")

    monkeypatch.setattr(github, "_run", fake_run)
    with pytest.raises(PreflightError, match="no GitHub remote"):
        github.preflight()


def test_preflight_passes(monkeypatch):
    monkeypatch.setattr(github.shutil, "which", lambda _: "/usr/local/bin/gh")
    monkeypatch.setattr(github, "_run", lambda args: _proc(stdout='{"name": "repo"}'))
    github.preflight()


def test_hung_binary_becomes_an_actionable_error(monkeypatch):
    def hang(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="gh", timeout=60)

    monkeypatch.setattr(subprocess, "run", hang)
    with pytest.raises(PreflightError, match="did not respond within"):
        github.detect_pr()


def test_absent_binary_becomes_an_actionable_error(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "git")

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(PreflightError, match="not found on PATH"):
        github.fetch_diff("42")


def test_detect_pr(monkeypatch):
    monkeypatch.setattr(github, "_run", lambda args: _proc(stdout='{"number": 42}'))
    assert github.detect_pr() == "42"


def test_detect_pr_none_open(monkeypatch):
    monkeypatch.setattr(github, "_run", lambda args: _proc(returncode=1, stderr="no pr"))
    with pytest.raises(PRNotFoundError, match="No open pull request"):
        github.detect_pr()


def test_detect_pr_unparseable(monkeypatch):
    monkeypatch.setattr(github, "_run", lambda args: _proc(stdout="not json"))
    with pytest.raises(PRNotFoundError, match="Could not read a PR number"):
        github.detect_pr()


def test_fetch_diff(monkeypatch):
    monkeypatch.setattr(github, "_run", lambda args: _proc(stdout="diff --git a/x b/x\n+hi\n"))
    assert "diff --git" in github.fetch_diff("42")


def test_fetch_diff_failure(monkeypatch):
    monkeypatch.setattr(github, "_run", lambda args: _proc(returncode=1, stderr="not found"))
    with pytest.raises(PRNotFoundError, match="Could not fetch the diff"):
        github.fetch_diff("999")


def test_fetch_diff_empty(monkeypatch):
    monkeypatch.setattr(github, "_run", lambda args: _proc(stdout="   \n"))
    with pytest.raises(PRNotFoundError, match="empty diff"):
        github.fetch_diff("42")


# --- parse_pr_url -----------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/octocat/Hello-World/pull/42",
        "http://github.com/octocat/Hello-World/pull/42",
        "github.com/octocat/Hello-World/pull/42",  # as pasted without a scheme
        "https://www.github.com/octocat/Hello-World/pull/42",
        "https://github.com/octocat/Hello-World/pull/42/",
        "https://github.com/octocat/Hello-World/pull/42/files",
        "https://github.com/octocat/Hello-World/pull/42?w=1",
        "  https://github.com/octocat/Hello-World/pull/42  ",
    ],
)
def test_parse_pr_url_accepts_real_world_forms(url):
    assert github.parse_pr_url(url) == ("octocat/Hello-World", "42")


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "https://gitlab.com/octocat/Hello-World/pull/42",
        "https://evil.com/octocat/Hello-World/pull/42",
        "https://github.com.evil.com/o/r/pull/1",
        "https://github.com/octocat/Hello-World",
        "https://github.com/octocat/Hello-World/issues/42",
        "not a url at all",
    ],
)
def test_parse_pr_url_rejects_everything_else(url):
    with pytest.raises(PRNotFoundError):
        github.parse_pr_url(url)


# --- is_repo_relative -------------------------------------------------------


@pytest.mark.parametrize("path", ["src/main.py", "a.py", "a/b/c.txt", "dir/..file.py"])
def test_is_repo_relative_accepts_in_repo_paths(path):
    assert github.is_repo_relative(path) is True


@pytest.mark.parametrize(
    "path",
    ["../etc/passwd", "a/../../b", "/etc/passwd", "..", "", "..\\x", "\\etc\\passwd", "a\\..\\..\\b"],
)
def test_is_repo_relative_refuses_escapes(path):
    assert github.is_repo_relative(path) is False


@pytest.mark.parametrize("path", ["C:/Windows/System32/config/SAM", "C:\\evil", "d:/x", "Z:"])
def test_is_repo_relative_refuses_windows_drive_paths(path):
    """`C:/x` has no leading separator and no `..`, but is absolute on Windows.

    Joining it to the repo root discards the root entirely, so the boundary
    guard has to reject it even though it looks relative on a POSIX host.
    """
    assert github.is_repo_relative(path) is False


# --- preflight, split by mode ----------------------------------------------


def test_preflight_with_repo_skips_the_git_check(monkeypatch):
    """--repo mode must work from any directory, so 'inside a git repo' cannot apply."""
    monkeypatch.setattr(github.shutil, "which", lambda _: "/usr/local/bin/gh")
    calls = _recorder(monkeypatch, result=_proc(stdout='{"name": "Hello-World"}'))

    github.preflight(repo="octocat/Hello-World")

    assert not any(args[0] == "git" for args in calls), f"ran git in repo mode: {calls}"
    assert ["gh", "repo", "view", "octocat/Hello-World", "--json", "name"] in calls


def test_preflight_with_unreachable_repo(monkeypatch):
    monkeypatch.setattr(github.shutil, "which", lambda _: "/usr/local/bin/gh")

    def results(args):
        if args[:3] == ["gh", "repo", "view"]:
            return _proc(returncode=1, stderr="Could not resolve to a Repository")
        return _proc()

    _recorder(monkeypatch, results=results)
    with pytest.raises(PreflightError, match="Cannot access the repository"):
        github.preflight(repo="octocat/nope")


def test_preflight_rejects_a_malformed_repo(monkeypatch):
    monkeypatch.setattr(github.shutil, "which", lambda _: "/usr/local/bin/gh")
    _recorder(monkeypatch)
    with pytest.raises(PRNotFoundError, match="not a valid repository"):
        github.preflight(repo="not-a-repo-shape")


# --- --repo threading -------------------------------------------------------


def test_detect_pr_passes_repo_through(monkeypatch):
    calls = _recorder(monkeypatch, result=_proc(stdout='{"number": 42}'))
    assert github.detect_pr(repo="octocat/Hello-World") == "42"
    assert "--repo" in calls[0] and "octocat/Hello-World" in calls[0]


def test_fetch_diff_passes_repo_through(monkeypatch):
    calls = _recorder(monkeypatch, result=_proc(stdout="diff --git a/x b/x\n"))
    github.fetch_diff("42", repo="octocat/Hello-World")
    assert "--repo" in calls[0] and "octocat/Hello-World" in calls[0]


def test_cwd_mode_sends_no_repo_flag(monkeypatch):
    calls = _recorder(monkeypatch, result=_proc(stdout="diff --git a/x b/x\n"))
    github.fetch_diff("42")
    assert "--repo" not in calls[0]


# --- fetch_pr_meta ----------------------------------------------------------

_META = json.dumps(
    {
        "number": 42,
        "title": "Add a thing",
        "author": {"login": "octocat"},
        "updatedAt": "2026-08-01T00:00:00Z",
        "headRefOid": "b" * 40,
        "state": "OPEN",
        "url": "https://github.com/octocat/Hello-World/pull/42",
    }
)


def test_fetch_pr_meta_flattens_the_payload(monkeypatch):
    _recorder(monkeypatch, result=_proc(stdout=_META))
    meta = github.fetch_pr_meta("42", repo="octocat/Hello-World")
    assert meta == {
        "number": "42",
        "title": "Add a thing",
        "author": "octocat",
        "updated_at": "2026-08-01T00:00:00Z",
        "head_sha": "b" * 40,
        "state": "OPEN",
        "url": "https://github.com/octocat/Hello-World/pull/42",
    }


def test_fetch_pr_meta_requires_a_head_sha(monkeypatch):
    """Without headRefOid the reader has no ref, and would silently read the default branch."""
    payload = json.loads(_META)
    del payload["headRefOid"]
    _recorder(monkeypatch, result=_proc(stdout=json.dumps(payload)))
    with pytest.raises(PRNotFoundError, match="head commit SHA"):
        github.fetch_pr_meta("42", repo="octocat/Hello-World")


def test_fetch_pr_meta_gh_failure(monkeypatch):
    _recorder(monkeypatch, result=_proc(returncode=1, stderr="no such PR"))
    with pytest.raises(PRNotFoundError, match="Could not read PR"):
        github.fetch_pr_meta("999", repo="octocat/Hello-World")


def test_fetch_pr_meta_unparseable(monkeypatch):
    _recorder(monkeypatch, result=_proc(stdout="not json"))
    with pytest.raises(PRNotFoundError, match="Could not parse"):
        github.fetch_pr_meta("42", repo="octocat/Hello-World")


# --- fetch_file_content -----------------------------------------------------


def _contents(text, encoding="base64"):
    body = base64.b64encode(text.encode("utf-8")).decode() if isinstance(text, str) else text
    return _proc(stdout=json.dumps({"content": body, "encoding": encoding}))


def test_fetch_file_content_decodes_base64(monkeypatch):
    _recorder(monkeypatch, result=_contents("hello\n"))
    assert github.fetch_file_content("octocat/Hello-World", "src/app.py", "abc123") == "hello\n"


def test_fetch_file_content_requests_the_given_ref(monkeypatch):
    """The ref is the whole point: the wrong one pairs the right diff with wrong files."""
    calls = _recorder(monkeypatch, result=_contents("x"))
    github.fetch_file_content("octocat/Hello-World", "src/app.py", "abc123")
    assert "?ref=abc123" in calls[0][-1]
    assert "repos/octocat/Hello-World/contents/src/app.py" in calls[0][-1]


def test_fetch_file_content_encodes_awkward_filenames(monkeypatch):
    calls = _recorder(monkeypatch, result=_contents("x"))
    github.fetch_file_content("octocat/Hello-World", "src/my file#1.py", "abc")
    url = calls[0][-1]
    assert "src/my%20file%231.py" in url, url


def test_fetch_file_content_refuses_traversal_without_requesting(monkeypatch):
    """Refused before the subprocess, so the user's gh credentials are never spent on it."""
    calls = _recorder(monkeypatch)
    with pytest.raises(ValueError, match="outside the repository"):
        github.fetch_file_content("octocat/Hello-World", "../../etc/passwd", "abc")
    assert calls == [], "a request went out for a path that should have been refused"


def test_fetch_file_content_rejects_a_malformed_repo(monkeypatch):
    calls = _recorder(monkeypatch)
    with pytest.raises(PRNotFoundError, match="not a valid repository"):
        github.fetch_file_content("notarepo", "a.py", "abc")
    assert calls == []


@pytest.mark.parametrize(
    "result",
    [
        _proc(returncode=1, stderr="404"),          # absent at this ref
        _proc(stdout="not json"),
        _proc(stdout='[{"name": "a.py"}]'),          # a directory
        _proc(stdout='{"content": "", "encoding": "none"}'),  # over 1 MB
    ],
    ids=["gh-failure", "unparseable", "directory", "too-large"],
)
def test_fetch_file_content_returns_none_rather_than_raising(monkeypatch, result):
    """An unreadable file is routine -- it lands in `missing` beside its diff hunk."""
    _recorder(monkeypatch, result=result)
    assert github.fetch_file_content("octocat/Hello-World", "a.py", "abc") is None


def test_fetch_file_content_returns_none_for_binary(monkeypatch):
    binary = base64.b64encode(b"\xff\xfe\x00\x01").decode()
    _recorder(monkeypatch, result=_proc(stdout=json.dumps({"content": binary, "encoding": "base64"})))
    assert github.fetch_file_content("octocat/Hello-World", "logo.png", "abc") is None


# --- large-diff fallback / --exclude ----------------------------------------


def test_fetch_diff_forwards_exclude_patterns_to_gh(monkeypatch):
    seen = {}

    def fake_run(args):
        seen["args"] = args
        return _proc(stdout="diff --git a/x b/x\n+hi\n")

    monkeypatch.setattr(github, "_run", fake_run)
    github.fetch_diff("42", exclude=("**/*.lock", "frontend/src/api/generated.ts"))
    assert seen["args"] == [
        "gh",
        "pr",
        "diff",
        "42",
        "--exclude",
        "**/*.lock",
        "--exclude",
        "frontend/src/api/generated.ts",
    ]


_REPO_URL = "https://github.com/octocat/Hello-World.git"


def test_fetch_diff_local_fallback_applies_exclude_pathspecs(monkeypatch):
    def fake_run(args):
        if args[:3] == ["gh", "pr", "diff"]:
            return _proc(returncode=1, stderr="too_large")
        if args[:3] == ["gh", "pr", "view"]:
            return _proc(stdout='{"baseRefName": "main"}')
        if args[:3] == ["gh", "repo", "view"]:
            return _proc(stdout=f'{{"url": "{_REPO_URL}"}}')
        if args[:2] == ["git", "fetch"]:
            assert args[2:] == [_REPO_URL, "main"]
            return _proc()
        if args[:2] == ["git", "diff"]:
            assert args[2:] == [
                "FETCH_HEAD...HEAD",
                "--",
                ".",
                ":(exclude)**/*.lock",
            ]
            return _proc(stdout="diff --git a/x b/x\n+hi\n")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(github, "_run", fake_run)
    assert "diff --git" in github.fetch_diff("2", exclude=("**/*.lock",))


def test_fetch_diff_falls_back_locally_when_too_large(monkeypatch):
    def fake_run(args):
        if args[:3] == ["gh", "pr", "diff"]:
            return _proc(returncode=1, stderr="HTTP 406: ... too_large")
        if args[:3] == ["gh", "pr", "view"]:
            return _proc(stdout='{"baseRefName": "main"}')
        if args[:3] == ["gh", "repo", "view"]:
            return _proc(stdout=f'{{"url": "{_REPO_URL}"}}')
        if args[:2] == ["git", "fetch"]:
            assert args[2:] == [_REPO_URL, "main"]
            return _proc()
        if args[:2] == ["git", "diff"]:
            assert args[2] == "FETCH_HEAD...HEAD"
            return _proc(stdout="diff --git a/x b/x\n+hi\n")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(github, "_run", fake_run)
    assert "diff --git" in github.fetch_diff("2")


def test_fetch_diff_local_fallback_works_when_origin_is_a_fork(monkeypatch):
    """The whole point of the fetch-by-URL fix: no local remote named `origin`

    is assumed to point at the right repo. Simulate `origin` being some other
    remote entirely -- the fallback must not reference it at all.
    """

    def fake_run(args):
        if args[:3] == ["gh", "pr", "diff"]:
            return _proc(returncode=1, stderr="too_large")
        if args[:3] == ["gh", "pr", "view"]:
            return _proc(stdout='{"baseRefName": "main"}')
        if args[:3] == ["gh", "repo", "view"]:
            return _proc(stdout=f'{{"url": "{_REPO_URL}"}}')
        if args[:2] == ["git", "fetch"]:
            assert "origin" not in args
            assert args[2:] == [_REPO_URL, "main"]
            return _proc()
        if args[:2] == ["git", "diff"]:
            assert "origin" not in " ".join(args)
            return _proc(stdout="diff --git a/x b/x\n+hi\n")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(github, "_run", fake_run)
    assert "diff --git" in github.fetch_diff("2")


def test_fetch_diff_local_fallback_base_lookup_fails(monkeypatch):
    def fake_run(args):
        if args[:3] == ["gh", "pr", "diff"]:
            return _proc(returncode=1, stderr="too_large")
        if args[:3] == ["gh", "pr", "view"]:
            return _proc(returncode=1, stderr="not found")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(github, "_run", fake_run)
    with pytest.raises(PRNotFoundError, match="base branch could not be looked up"):
        github.fetch_diff("2")


def test_fetch_diff_local_fallback_repo_url_lookup_fails(monkeypatch):
    def fake_run(args):
        if args[:3] == ["gh", "pr", "diff"]:
            return _proc(returncode=1, stderr="too_large")
        if args[:3] == ["gh", "pr", "view"]:
            return _proc(stdout='{"baseRefName": "main"}')
        if args[:3] == ["gh", "repo", "view"]:
            return _proc(returncode=1, stderr="not a repository")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(github, "_run", fake_run)
    with pytest.raises(PRNotFoundError, match="URL could not be resolved"):
        github.fetch_diff("2")


def test_fetch_diff_local_fallback_fetch_fails(monkeypatch):
    def fake_run(args):
        if args[:3] == ["gh", "pr", "diff"]:
            return _proc(returncode=1, stderr="too_large")
        if args[:3] == ["gh", "pr", "view"]:
            return _proc(stdout='{"baseRefName": "main"}')
        if args[:3] == ["gh", "repo", "view"]:
            return _proc(stdout=f'{{"url": "{_REPO_URL}"}}')
        if args[:2] == ["git", "fetch"]:
            return _proc(returncode=1, stderr="could not resolve host")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(github, "_run", fake_run)
    with pytest.raises(PRNotFoundError, match=r"git fetch .*main.*failed"):
        github.fetch_diff("2")


def test_fetch_diff_local_fallback_diff_fails(monkeypatch):
    def fake_run(args):
        if args[:3] == ["gh", "pr", "diff"]:
            return _proc(returncode=1, stderr="too_large")
        if args[:3] == ["gh", "pr", "view"]:
            return _proc(stdout='{"baseRefName": "main"}')
        if args[:3] == ["gh", "repo", "view"]:
            return _proc(stdout=f'{{"url": "{_REPO_URL}"}}')
        if args[:2] == ["git", "fetch"]:
            return _proc()
        if args[:2] == ["git", "diff"]:
            return _proc(returncode=1, stderr="unknown revision")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(github, "_run", fake_run)
    with pytest.raises(PRNotFoundError, match="local fallback"):
        github.fetch_diff("2")


def test_fetch_diff_local_fallback_empty(monkeypatch):
    def fake_run(args):
        if args[:3] == ["gh", "pr", "diff"]:
            return _proc(returncode=1, stderr="too_large")
        if args[:3] == ["gh", "pr", "view"]:
            return _proc(stdout='{"baseRefName": "main"}')
        if args[:3] == ["gh", "repo", "view"]:
            return _proc(stdout=f'{{"url": "{_REPO_URL}"}}')
        if args[:2] == ["git", "fetch"]:
            return _proc()
        if args[:2] == ["git", "diff"]:
            return _proc(stdout="   \n")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(github, "_run", fake_run)
    with pytest.raises(PRNotFoundError, match="nothing to review locally"):
        github.fetch_diff("2")

import subprocess

import pytest

from tri_review import github
from tri_review.errors import PreflightError, PRNotFoundError


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


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

"""PR access via the `gh` CLI.

Using `gh` rather than the GitHub API means auth is whatever the user already
set up with `gh auth login`, and branch->PR detection comes for free.
"""

from __future__ import annotations

import json
import shutil
import subprocess

from .errors import PreflightError, PRNotFoundError

_GH_TIMEOUT = 60


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=_GH_TIMEOUT)


def preflight() -> None:
    """Verify gh is installed, authenticated, and pointed at a GitHub repo.

    Raises PreflightError with an actionable message for each failure mode.
    """
    if shutil.which("gh") is None:
        raise PreflightError(
            "GitHub CLI (gh) not found on PATH.\n"
            "Install it from https://cli.github.com, then run: gh auth login"
        )

    git_check = _run(["git", "rev-parse", "--git-dir"])
    if git_check.returncode != 0:
        raise PreflightError(
            "Not inside a git repository.\n"
            "Run tri-review from the root of the repo whose PR you want reviewed."
        )

    auth = _run(["gh", "auth", "status"])
    if auth.returncode != 0:
        raise PreflightError(
            "GitHub CLI is not authenticated.\nRun: gh auth login"
        )

    repo = _run(["gh", "repo", "view", "--json", "name"])
    if repo.returncode != 0:
        raise PreflightError(
            "This repository has no GitHub remote that gh can resolve.\n"
            f"gh said: {repo.stderr.strip() or 'no detail'}"
        )


def detect_pr() -> str:
    """Return the PR number open for the current branch."""
    result = _run(["gh", "pr", "view", "--json", "number"])
    if result.returncode != 0:
        raise PRNotFoundError(
            "No open pull request found for the current branch.\n"
            "Pass one explicitly with: tri-review --pr <number>"
        )
    try:
        number = json.loads(result.stdout)["number"]
    except (json.JSONDecodeError, KeyError, TypeError):
        raise PRNotFoundError(
            f"Could not read a PR number from gh output: {result.stdout.strip()!r}"
        ) from None
    return str(number)


def fetch_diff(pr_number: str) -> str:
    """Return the unified diff for a pull request."""
    result = _run(["gh", "pr", "diff", str(pr_number)])
    if result.returncode != 0:
        raise PRNotFoundError(
            f"Could not fetch the diff for PR #{pr_number}.\n"
            f"gh said: {result.stderr.strip() or 'no detail'}"
        )
    if not result.stdout.strip():
        raise PRNotFoundError(f"PR #{pr_number} has an empty diff -- nothing to review.")
    return result.stdout

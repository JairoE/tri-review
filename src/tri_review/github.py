"""PR access via the `gh` CLI.

Using `gh` rather than the GitHub API means auth is whatever the user already
set up with `gh auth login`, and branch->PR detection comes for free.

Every read takes an optional `repo` ("owner/name"). With it, the caller can
address any repo it has access to from any directory; without it, `gh` falls
back to the repo the process is sitting in, which is how the CLI has always
worked.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import shutil
import subprocess
from urllib.parse import quote, urlparse

from .errors import PreflightError, PRNotFoundError

_GH_TIMEOUT = 60

# GitHub owner/name rules, tightened: no leading dot or dash, no path separators.
# This value is interpolated into a `gh api` URL path, so it is validated rather
# than trusted -- see `_require_repo`.
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")


def _run(args: list[str]) -> subprocess.CompletedProcess:
    """Run a helper binary, turning the two ways it can fail to run into errors.

    A non-zero exit is the caller's business; a hung or absent binary is not, and
    must not reach the user as a traceback.
    """
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=_GH_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise PreflightError(
            f"`{' '.join(args)}` did not respond within {_GH_TIMEOUT}s.\n"
            "Check your network connection and that GitHub is reachable."
        ) from None
    except FileNotFoundError:
        raise PreflightError(
            f"`{args[0]}` not found on PATH.\ntri-review needs both `git` and `gh` installed."
        ) from None


def _require_repo(repo: str) -> str:
    """Return `repo` if it is a well-formed owner/name, else raise.

    `repo` reaches `gh api repos/{repo}/...` as raw URL path, so a value like
    `octocat/Hello-World/../../user` would address a different endpoint entirely.
    Validating the shape once here is cheaper than escaping at each call site.
    """
    if not _REPO_RE.match(repo or ""):
        raise PRNotFoundError(
            f"{repo!r} is not a valid repository. Expected the form owner/name, "
            "for example octocat/Hello-World."
        )
    return repo


def _repo_args(repo: str | None) -> list[str]:
    """The `--repo owner/name` pair when addressing a specific repo, else nothing."""
    return ["--repo", _require_repo(repo)] if repo else []


def is_repo_relative(path: str) -> bool:
    """True if `path` stays inside the repository it claims to belong to.

    Diff paths are written by whoever opened the PR, so they are untrusted, and
    they end up both as filesystem reads and as `gh api` requests made with the
    reviewer's own credentials. An absolute path or any `..` segment escapes the
    repo in both cases and is refused before either happens.
    """
    if not path or path.startswith("/") or path.startswith("\\"):
        return False
    # `C:/Windows/...` has no leading separator and no `..`, but on a Windows
    # host it is still absolute: joining it to the repo root discards the root.
    if re.match(r"^[A-Za-z]:", path):
        return False
    parts = re.split(r"[/\\]", path)
    return ".." not in parts


def parse_pr_url(url: str) -> tuple[str, str]:
    """Split a GitHub pull request URL into ("owner/name", "number").

    Accepts the URL as copied from the browser address bar, with or without a
    scheme, a trailing slash, a query string, or a sub-page such as `/files`.
    """
    raw = (url or "").strip()
    if not raw:
        raise PRNotFoundError("No pull request URL given.")

    # urlparse only finds a netloc when a scheme is present, and users paste
    # bare `github.com/...` at least as often as the full URL.
    parsed = urlparse(raw if "://" in raw else f"https://{raw}")
    host = parsed.netloc.split("@")[-1].split(":")[0].lower()
    if host not in ("github.com", "www.github.com"):
        raise PRNotFoundError(
            f"{url!r} is not a github.com pull request URL.\n"
            "Expected something like https://github.com/octocat/Hello-World/pull/42"
        )

    match = re.match(
        r"^/([A-Za-z0-9][A-Za-z0-9._-]*)/([A-Za-z0-9][A-Za-z0-9._-]*)/pulls?/(\d+)",
        parsed.path,
    )
    if not match:
        raise PRNotFoundError(
            f"Could not find a pull request number in {url!r}.\n"
            "Expected something like https://github.com/octocat/Hello-World/pull/42"
        )

    owner, name, number = match.groups()
    return f"{owner}/{name}", number


def preflight(repo: str | None = None) -> None:
    """Verify gh is installed, authenticated, and can see the target repo.

    In cwd-mode (`repo` is None) the target is whatever repo the process is in,
    so being inside a git checkout is part of the contract. With an explicit
    `repo` that check does not apply -- the whole point is to work from
    anywhere -- and the repo itself is resolved instead.
    """
    if shutil.which("gh") is None:
        raise PreflightError(
            "GitHub CLI (gh) not found on PATH.\n"
            "Install it from https://cli.github.com, then run: gh auth login"
        )

    if repo is None:
        git_check = _run(["git", "rev-parse", "--git-dir"])
        if git_check.returncode != 0:
            raise PreflightError(
                "Not inside a git repository.\n"
                "Run tri-review from the root of the repo whose PR you want reviewed, "
                "or name one explicitly with --repo owner/name."
            )

    auth = _run(["gh", "auth", "status"])
    if auth.returncode != 0:
        raise PreflightError(
            "GitHub CLI is not authenticated.\nRun: gh auth login"
        )

    if repo is None:
        resolved = _run(["gh", "repo", "view", "--json", "name"])
        if resolved.returncode != 0:
            raise PreflightError(
                "This repository has no GitHub remote that gh can resolve.\n"
                f"gh said: {resolved.stderr.strip() or 'no detail'}"
            )
    else:
        resolved = _run(["gh", "repo", "view", _require_repo(repo), "--json", "name"])
        if resolved.returncode != 0:
            raise PreflightError(
                f"Cannot access the repository {repo}.\n"
                "Check the spelling, and that it exists and is visible to your gh account.\n"
                f"gh said: {resolved.stderr.strip() or 'no detail'}"
            )


def detect_pr(repo: str | None = None) -> str:
    """Return the PR number open for the current branch."""
    result = _run(["gh", "pr", "view", "--json", "number", *_repo_args(repo)])
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


def fetch_diff(pr_number: str, repo: str | None = None, exclude: tuple[str, ...] = ()) -> str:
    """Return the unified diff for a pull request, minus any excluded paths.

    `gh pr diff` goes through GitHub's diff API, which refuses to serve diffs
    over ~20,000 lines (HTTP 406, `too_large`). In cwd mode (no `repo`), fall
    back to a local `git diff` against the PR's base branch -- the PR's head is
    already required to be the current checkout (see README), so this
    reproduces the same three-dot diff GitHub would otherwise have returned.
    In --repo mode there is no local checkout to fall back to, so the error
    instead points at --exclude as the way to shrink the diff.
    """
    args = ["gh", "pr", "diff", str(pr_number), *_repo_args(repo)]
    for pattern in exclude:
        args += ["--exclude", pattern]
    result = _run(args)
    if result.returncode != 0:
        if _is_diff_too_large(result.stderr):
            if repo is None:
                return _fetch_diff_locally(pr_number, exclude)
            raise PRNotFoundError(
                f"PR #{pr_number}'s diff is too large for GitHub's API (over "
                "~20,000 lines). There is no local checkout to fall back to in "
                "--repo mode, so narrow it instead: --exclude '**/*.lock', "
                "for example."
            )
        raise PRNotFoundError(
            f"Could not fetch the diff for PR #{pr_number}.\n"
            f"gh said: {result.stderr.strip() or 'no detail'}"
        )
    if not result.stdout.strip():
        raise PRNotFoundError(f"PR #{pr_number} has an empty diff -- nothing to review.")
    return result.stdout


def fetch_pr_meta(pr_number: str, repo: str | None = None) -> dict:
    """Return number, title, author, updatedAt and headRefOid for a pull request.

    `headRefOid` is the load-bearing field: it is the commit the diff was taken
    against, and reading file contents at any other ref pairs the right diff with
    the wrong file bodies -- a silent quality failure rather than an error.
    """
    fields = "number,title,author,updatedAt,headRefOid,state,url"
    result = _run(
        ["gh", "pr", "view", str(pr_number), "--json", fields, *_repo_args(repo)]
    )
    if result.returncode != 0:
        raise PRNotFoundError(
            f"Could not read PR #{pr_number}"
            + (f" in {repo}" if repo else "")
            + f".\ngh said: {result.stderr.strip() or 'no detail'}"
        )
    try:
        meta = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise PRNotFoundError(
            f"Could not parse gh output for PR #{pr_number}: {result.stdout.strip()!r}"
        ) from None

    if not meta.get("headRefOid"):
        raise PRNotFoundError(
            f"PR #{pr_number} came back without a head commit SHA, so its files "
            "cannot be read at the reviewed revision."
        )

    author = meta.get("author") or {}
    return {
        "number": str(meta.get("number", pr_number)),
        "title": meta.get("title") or "",
        "author": author.get("login") or "",
        "updated_at": meta.get("updatedAt") or "",
        "head_sha": meta["headRefOid"],
        "state": meta.get("state") or "",
        "url": meta.get("url") or "",
    }


def fetch_file_content(repo: str, path: str, ref: str) -> str | None:
    """Return a file's text at `ref`, or None if it is absent, binary, or too large.

    None rather than an exception: a file the reviewer cannot read is reported as
    missing alongside its diff hunk, exactly as an unreadable local file is. Only
    a path that tries to escape the repo is an error, because that is a caller
    bug or an attack, not a fact about the repository.
    """
    _require_repo(repo)
    if not is_repo_relative(path):
        raise ValueError(f"refusing to request a path outside the repository: {path!r}")

    # Each segment is quoted separately so that slashes stay structural while
    # spaces, '#' and '?' inside a filename do not alter the request.
    encoded = "/".join(quote(segment, safe="") for segment in path.split("/"))
    result = _run(
        ["gh", "api", f"repos/{repo}/contents/{encoded}?ref={quote(ref, safe='')}"]
    )
    if result.returncode != 0:
        return None

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    # A directory comes back as a list, and files over 1 MB come back with an
    # empty body and encoding "none".
    if not isinstance(payload, dict) or payload.get("encoding") != "base64":
        return None

    try:
        return base64.b64decode(payload.get("content") or "").decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None  # binary, or not valid UTF-8 text


def _is_diff_too_large(stderr: str) -> bool:
    lowered = stderr.lower()
    return "too_large" in lowered or "maximum number of lines" in lowered


def _fetch_diff_locally(pr_number: str, exclude: tuple[str, ...] = ()) -> str:
    """Compute a large PR's diff locally instead of through GitHub's diff API.

    Fetches directly from the repo's clone URL (resolved via `gh repo view`,
    which is fork-aware and defaults to the parent repo) rather than assuming
    a local remote is named `origin` and points at the right repo. A fork
    workflow (`origin` = your fork, `upstream` = the real repo) or any
    differently-named remote would otherwise fetch the wrong content, or
    nothing at all.
    """
    view = _run(["gh", "pr", "view", str(pr_number), "--json", "baseRefName"])
    if view.returncode != 0:
        raise PRNotFoundError(
            f"PR #{pr_number}'s diff is too large for GitHub's API, and its base "
            "branch could not be looked up to compute it locally.\n"
            f"gh said: {view.stderr.strip() or 'no detail'}"
        )
    try:
        base_ref = json.loads(view.stdout)["baseRefName"]
    except (json.JSONDecodeError, KeyError, TypeError):
        raise PRNotFoundError(
            f"Could not read the base branch from gh output: {view.stdout.strip()!r}"
        ) from None

    repo_view = _run(["gh", "repo", "view", "--json", "url"])
    if repo_view.returncode != 0:
        raise PRNotFoundError(
            f"PR #{pr_number}'s diff is too large for GitHub's API, and this "
            "repository's URL could not be resolved to compute it locally.\n"
            f"gh said: {repo_view.stderr.strip() or 'no detail'}"
        )
    try:
        repo_url = json.loads(repo_view.stdout)["url"]
    except (json.JSONDecodeError, KeyError, TypeError):
        raise PRNotFoundError(
            f"Could not read this repository's URL from gh output: "
            f"{repo_view.stdout.strip()!r}"
        ) from None

    fetch = _run(["git", "fetch", repo_url, base_ref])
    if fetch.returncode != 0:
        raise PRNotFoundError(
            f"PR #{pr_number}'s diff is too large for GitHub's API, and "
            f"`git fetch {repo_url} {base_ref}` failed, so it could not be "
            f"computed locally either.\ngit said: {fetch.stderr.strip() or 'no detail'}"
        )

    diff_args = ["git", "diff", "FETCH_HEAD...HEAD"]
    if exclude:
        diff_args.append("--")
        diff_args.append(".")
        diff_args += [f":(exclude){pattern}" for pattern in exclude]
    diff = _run(diff_args)
    if diff.returncode != 0:
        raise PRNotFoundError(
            f"PR #{pr_number}'s diff is too large for GitHub's API, and the "
            f"local fallback (`{' '.join(diff_args)}`) failed.\n"
            f"git said: {diff.stderr.strip() or 'no detail'}\n"
            "This fallback requires the PR's own branch to be the current "
            "checkout -- see README."
        )
    if not diff.stdout.strip():
        raise PRNotFoundError(
            f"PR #{pr_number} has an empty diff against {repo_url}@{base_ref} -- "
            "nothing to review locally. This fallback requires the PR's head "
            "branch to be checked out (see README); if it isn't, this diff "
            "won't match the PR, or --exclude dropped every changed file."
        )
    return diff.stdout

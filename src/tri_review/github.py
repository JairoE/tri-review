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

from . import gating
from .errors import NothingToReview, PreflightError, PRNotFoundError

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


def fetch_changed_files(pr_number: str, repo: str | None = None) -> tuple[list[str], bool]:
    """Return (paths, complete) for a PR, without fetching the diff body.

    Cheap enough to run before deciding whether a review is worth buying: it is
    one metadata call, where the diff is a second call and the file contents are
    one network read per changed file on top of that.

    `complete` says whether the list is the whole story. GitHub's GraphQL file
    connection is paginated and `gh pr view --json files` returns one page of it,
    so the count is checked against `changedFiles`, which the PR reports for
    itself. An earlier version compared the length to a hardcoded page size of
    100 instead -- which is safe only if that guess is not larger than the real
    page size, and is a silent skip of unseen code if it is. Asking the PR how
    many files it changed needs no guess at all.

    Anything short of a confirmed match reads as incomplete, because the cost of
    the two mistakes is not symmetric: reviewing a PR that did not need it wastes
    money, while skipping one that did is the failure this tool exists to prevent.
    """
    result = _run(
        [
            "gh", "pr", "view", str(pr_number),
            "--json", "files,changedFiles",
            *_repo_args(repo),
        ]
    )
    if result.returncode != 0:
        raise PRNotFoundError(
            f"Could not list the files changed by PR #{pr_number}"
            + (f" in {repo}" if repo else "")
            + f".\ngh said: {result.stderr.strip() or 'no detail'}"
        )
    try:
        payload = json.loads(result.stdout)
        files = payload.get("files") or []
        paths = [str(f["path"]) for f in files if f.get("path")]
    except (json.JSONDecodeError, AttributeError, TypeError, KeyError):
        raise PRNotFoundError(
            f"Could not read a file list from gh output: {result.stdout.strip()!r}"
        ) from None

    total = payload.get("changedFiles")
    complete = isinstance(total, int) and not isinstance(total, bool) and len(paths) == total
    return paths, complete


def working_tree_reason(head_sha: str) -> str | None:
    """Why the local checkout is not exactly `head_sha`, or None if it is.

    Only meaningful in cwd mode, where file bodies are read from the working tree
    rather than from GitHub. The stored review is keyed on the PR's head SHA, so
    replaying it is only sound if the tree those bodies came from *was* that
    commit. Review a PR from the wrong branch once and, without this, the wrong
    review is cached under the right SHA and replayed long after the mistake is
    fixed -- a caching layer turning a transient error into a durable one.

    Untracked files are deliberately ignored. If HEAD is the PR head then every
    path in the diff is tracked at that commit, so an untracked file cannot be
    one of them; counting them would block replay for anyone with a stray
    scratch directory, which is nearly everyone.
    """
    head = _run(["git", "rev-parse", "HEAD"])
    if head.returncode != 0:
        return "the local HEAD could not be read"
    local = head.stdout.strip()
    if local != head_sha:
        return f"the checkout is at {local[:8]}, not the PR head {head_sha[:8]}"

    dirty = _run(["git", "status", "--porcelain", "--untracked-files=no"])
    if dirty.returncode != 0:
        return "the working tree status could not be read"
    if dirty.stdout.strip():
        return "the working tree has uncommitted changes"
    return None


def fetch_diff(pr_number: str, repo: str | None = None, exclude: tuple[str, ...] = ()) -> str:
    """Return the unified diff for a pull request, minus any excluded paths.

    `gh pr diff` goes through GitHub's diff API, which refuses to serve diffs
    over ~20,000 lines (HTTP 406, `too_large`). In cwd mode (no `repo`), fall
    back to a local `git diff` against the PR's base branch -- the PR's head is
    already required to be the current checkout (see README), so this
    reproduces the same three-dot diff GitHub would otherwise have returned.
    In --repo mode there is no local checkout to fall back to, so the error
    instead points at --exclude as the way to shrink the diff.

    Excluded paths are filtered out here, by `gating.filter_diff`, not by
    `gh pr diff --exclude`: gh reads globs by Go's rules, not ours, and kept
    files the skip report said were dropped. gh filters client-side after the
    full diff arrives anyway, so handing it the patterns never avoided a 406.
    """
    args = ["gh", "pr", "diff", str(pr_number), *_repo_args(repo)]
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
    diff = gating.filter_diff(result.stdout, exclude)
    if not diff.strip():
        raise NothingToReview(
            f"PR #{pr_number} has an empty diff"
            + (" once the exclude patterns are applied." if exclude else "."),
            gate="empty-diff",
        )
    return diff


def fetch_pr_meta(pr_number: str, repo: str | None = None) -> dict:
    """Return number, title, author, updatedAt and headRefOid for a pull request.

    `headRefOid` is the load-bearing field: it is the commit the diff was taken
    against, and reading file contents at any other ref pairs the right diff with
    the wrong file bodies -- a silent quality failure rather than an error.
    """
    fields = "number,title,author,updatedAt,headRefOid,baseRefOid,state,url"
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
        # The merge base, not the PR's own commits. The dismissal ledger is
        # read at this ref so a PR cannot add entries that excuse its own
        # findings -- see dismissals and graph.fetch_context_node.
        "base_sha": meta.get("baseRefOid") or "",
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
    return (
        "too_large" in lowered
        or "maximum number of lines" in lowered
        or "taking too long to generate" in lowered
    )


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

    # Excludes are applied to the output, not as `:(exclude)` pathspecs, for
    # the same reason as in fetch_diff: git's glob rules are not ours either.
    diff_args = ["git", "diff", "FETCH_HEAD...HEAD"]
    diff = _run(diff_args)
    if diff.returncode != 0:
        raise PRNotFoundError(
            f"PR #{pr_number}'s diff is too large for GitHub's API, and the "
            f"local fallback (`{' '.join(diff_args)}`) failed.\n"
            f"git said: {diff.stderr.strip() or 'no detail'}\n"
            "This fallback requires the PR's own branch to be the current "
            "checkout -- see README."
        )
    filtered = gating.filter_diff(diff.stdout, exclude)
    if not filtered.strip():
        # NOT a NothingToReview. Reaching here means GitHub refused this diff for
        # being over ~20,000 lines, and the local recomputation of that same diff
        # came back empty -- a contradiction. The only ways to produce it are a
        # wrong checkout (the usual one: running `--pr N` from the base branch)
        # or excludes that dropped all 20,000 lines. Exiting 0 here would post
        # "Nothing to review" on the largest PR the tool ever sees.
        raise PRNotFoundError(
            f"PR #{pr_number}'s diff is too large for GitHub's API, and the "
            f"local fallback produced an empty diff against {repo_url}@{base_ref}. "
            "A PR that large cannot have an empty diff, so this is not a PR with "
            "nothing in it: either the PR's head branch is not the current "
            "checkout (see README), or --exclude dropped every changed file."
        )
    return filtered

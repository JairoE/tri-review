"""Turn a PR diff into a review payload: the diff plus full contents of the files it touches."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import config, github

# Reads one repository-relative path and returns its text, or None if there is
# nothing readable there. Never receives a path that escapes the repo textually
# -- that is filtered at the boundary in `build_context` before any reader is
# called. A reader that discovers an escape only it can see raises PathOutsideRepo.
Reader = Callable[[str], "str | None"]


class PathOutsideRepo(Exception):
    """A reader was asked for a path whose real location is outside the repository.

    Distinct from returning None: None means "nothing readable here", which is
    routine, while this means "readable, but not yours", which the user is told
    about loudly.
    """


@dataclass
class ReviewContext:
    diff: str
    files: dict[str, str]
    dropped: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    # Tokens by which the diff alone overruns the budget, 0 if it fits.
    diff_overflow: int = 0

    @property
    def estimated_tokens(self) -> int:
        return config.estimate_tokens(self.render())

    def render(self) -> str:
        parts = [f"## Pull request diff\n\n```diff\n{self.diff}\n```"]
        for path, content in self.files.items():
            parts.append(f"## Current contents of `{path}`\n\n```\n{content}\n```")
        if self.dropped:
            parts.append(
                "## Note\n\nFull contents of these changed files were omitted to fit the "
                "context budget; only their diff hunks above are available: "
                + ", ".join(f"`{p}`" for p in self.dropped)
            )
        return "\n\n".join(parts)


def preview_context(
    pr_number: str, repo: str | None = None, exclude: tuple[str, ...] = ()
) -> ReviewContext:
    """Build the review context for a PR without calling any model.

    The same work `fetch_context_node` does, minus the graph. Reviews cost real
    money, so both the CLI's `--dry-run` and the web app's confirm step need to
    show what would be sent before anything is spent.
    """
    diff = github.fetch_diff(pr_number, repo, exclude)
    if repo:
        head_sha = github.fetch_pr_meta(pr_number, repo)["head_sha"]
        reader = github_reader(repo, head_sha)
    else:
        reader = filesystem_reader(Path.cwd())
    return build_context(diff, reader=reader)


def parse_changed_files(diff: str) -> list[str]:
    """Return post-image paths of files the diff touches, excluding deletions.

    Reads the `+++ b/path` header rather than the `diff --git` line: the latter is
    ambiguous for paths containing spaces, and `+++ /dev/null` cleanly marks a
    deletion (whose content there is no point reading from disk).
    """
    paths: list[str] = []
    for line in diff.splitlines():
        if not line.startswith("+++ "):
            continue
        target = line[4:].split("\t", 1)[0].strip()
        if target == "/dev/null":
            continue
        if target.startswith("b/"):
            target = target[2:]
        if target and target not in paths:
            paths.append(target)
    return paths


def _is_inside(path: Path, root: Path) -> bool:
    """True if `path` resolves to a location inside `root`, symlinks followed.

    The textual guard in `github.is_repo_relative` already rejected `..` and
    absolute paths at the boundary. This catches the case only a filesystem can
    have: a symlink the PR adds and the user then checks out, whose *target*
    leaves the repo even though its path never says so.
    """
    try:
        return path.resolve().is_relative_to(root)
    except OSError:  # symlink loop, or a path the OS refuses to resolve
        return False


def filesystem_reader(root: Path | None = None) -> Reader:
    """Read changed files from a local checkout at `root` (default: cwd)."""
    base = (root or Path.cwd()).resolve()

    def read(rel: str) -> str | None:
        path = base / rel
        if not _is_inside(path, base):
            raise PathOutsideRepo(rel)
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # Binary, generated-then-deleted, or outside the checkout. The diff
            # hunk is still in the payload, so the reviewer isn't blind to it.
            return None

    return read


def github_reader(repo: str, ref: str) -> Reader:
    """Read changed files from GitHub at a specific ref.

    `ref` must be the PR's head SHA. A branch name would drift, and the default
    branch would pair the right diff with the wrong file bodies.
    """

    def read(rel: str) -> str | None:
        return github.fetch_file_content(repo, rel, ref)

    return read


def build_context(
    diff: str,
    root: Path | None = None,
    budget: int | None = None,
    reader: Reader | None = None,
) -> ReviewContext:
    """Assemble diff + changed-file contents, dropping largest files to fit the budget.

    `reader` decides where file contents come from; `root` is a shorthand for a
    filesystem reader at that path, kept so existing callers and tests are
    unaffected.
    """
    budget = budget if budget is not None else config.token_budget()
    if reader is None:
        reader = filesystem_reader(root)

    ctx = ReviewContext(diff=diff, files={})
    candidates: list[tuple[str, str]] = []

    for rel in parse_changed_files(diff):
        # Refused once, here, for every reader. Diff paths are written by whoever
        # opened the PR: a `+++ b/../../.ssh/id_rsa` header must not pull a file
        # off the reviewer's disk, nor spend their GitHub credentials on a
        # request to somewhere else, into a payload bound for three third-party
        # APIs. Doing this at the boundary means a new reader inherits the
        # refusal instead of having to remember it.
        if not github.is_repo_relative(rel):
            ctx.rejected.append(rel)
            continue
        try:
            content = reader(rel)
        except PathOutsideRepo:
            ctx.rejected.append(rel)
            continue
        if content is None:
            ctx.missing.append(rel)
        else:
            candidates.append((rel, content))

    # The diff is mandatory; files compete for whatever budget is left.
    remaining = budget - config.estimate_tokens(diff)
    if remaining < 0:
        ctx.diff_overflow = -remaining
    for rel, content in sorted(candidates, key=lambda item: len(item[1])):
        cost = config.estimate_tokens(content)
        if cost <= remaining:
            ctx.files[rel] = content
            remaining -= cost
        else:
            ctx.dropped.append(rel)

    return ctx

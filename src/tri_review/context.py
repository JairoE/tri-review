"""Turn a PR diff into a review payload: the diff plus full contents of the files it touches."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import config


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

    Diff paths are written by whoever opened the PR, so they are untrusted. A
    `+++ b/../../.ssh/id_rsa` header, or a symlink the PR adds and the user then
    checks out, must not pull a file off the reviewer's disk into a payload
    bound for three third-party APIs.
    """
    try:
        return path.resolve().is_relative_to(root)
    except OSError:  # symlink loop, or a path the OS refuses to resolve
        return False


def build_context(diff: str, root: Path | None = None, budget: int | None = None) -> ReviewContext:
    """Assemble diff + changed-file contents, dropping largest files to fit the budget."""
    root = root or Path.cwd()
    budget = budget if budget is not None else config.token_budget()
    root_resolved = root.resolve()

    ctx = ReviewContext(diff=diff, files={})
    candidates: list[tuple[str, str]] = []

    for rel in parse_changed_files(diff):
        path = root / rel
        if not _is_inside(path, root_resolved):
            ctx.rejected.append(rel)
            continue
        try:
            candidates.append((rel, path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            # Binary, generated-then-deleted, or outside the checkout. The diff
            # hunk is still in the payload, so the reviewer isn't blind to it.
            ctx.missing.append(rel)

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

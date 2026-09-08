"""Path gates: decide what is worth reviewing before anything is spent.

A review costs three model calls against a payload that can run to 100k tokens,
so the cheapest possible win is not making them at all. These gates answer
"is this worth reviewing?" from the changed-file list alone -- no diff body, no
file contents, no provider calls.

Only exact, path-based rules live here. A glob cannot be wrong about whether
`README.md` is Markdown, which is what makes it safe to skip on automatically.
Heuristics that guess at *intent* -- "this looks comment-only" -- are a
different thing with a different failure mode (silently not reviewing real
code) and do not belong behind an automatic skip.
"""

from __future__ import annotations

import re
from functools import lru_cache


@lru_cache(maxsize=256)
def _compile(pattern: str) -> re.Pattern[str]:
    """Translate a path glob to a regex.

    Semantics chosen to match what people already write in `--exclude` and in
    `.gitignore`, which is not what `fnmatch` does on its own:

    - `**/` spans zero or more directories, so `**/*.md` matches `README.md`
      as well as `docs/guide.md`. `fnmatch` would require the slash to exist.
    - `*` and `?` stop at a path separator, so `src/*.py` does not reach into
      `src/pkg/mod.py`.
    - A pattern with no `/` at all matches the basename at any depth, so
      `--exclude '*.lock'` behaves the way its author expects rather than
      silently matching only the repository root.
    """
    if "/" not in pattern:
        pattern = "**/" + pattern

    out = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(char))
            i += 1
    return re.compile("".join(out) + r"\Z")


def matches_any(path: str, patterns: tuple[str, ...]) -> bool:
    """True if `path` matches at least one glob in `patterns`."""
    return any(_compile(p).match(path) for p in patterns)


def partition(
    paths: list[str], patterns: tuple[str, ...]
) -> tuple[list[str], list[str]]:
    """Split `paths` into (reviewable, excluded), preserving order.

    Returned rather than reduced to a boolean because the user is told exactly
    which files were dropped and why. A gate that skips a review without naming
    what it skipped is indistinguishable from a gate that is broken.
    """
    reviewable: list[str] = []
    excluded: list[str] = []
    for path in paths:
        (excluded if matches_any(path, patterns) else reviewable).append(path)
    return reviewable, excluded

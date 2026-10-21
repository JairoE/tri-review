"""Which fixes can be posted as inline GitHub suggested changes (R7, R8).

GitHub only accepts a review comment on lines the PR's diff shows, and a
multi-line suggestion must stay inside one hunk. `parse_hunks` reads those
ranges from the unified diff; `applyability` checks a finding against them.
`build_review_payload` turns the applyable findings into the body of one
`POST /repos/{owner}/{repo}/pulls/{n}/reviews` call. Pure functions: no LLM,
no I/O.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from tri_review import gating
from tri_review.report import _stated, fence_for, render_block
from tri_review.schema import NormalizedFinding

MAX_SUGGESTION_LINES = 40

NO_REPLACEMENT = "no replacement code"
NOT_IN_DIFF = "file not in this diff"
FILE_DELETED = "file deleted in this PR"
OUTSIDE_DIFF = "lines outside the diff"
SPANS_HUNKS = "range spans more than one hunk"
TOO_LONG = f"range longer than {MAX_SUGGESTION_LINES} lines"
PREVIOUSLY_REJECTED = "previously rejected"
NO_REPORTER = "no reviewer reported it"

# What a hunk body line can start with; "" is a blank context line some tools
# strip, and "\\" is the "No newline at end of file" marker.
_BODY_PREFIXES = ("", " ", "+", "-", "\\")

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class FileHunks:
    """One file's hunks: new-side inclusive line ranges, in diff order."""

    deleted: bool = False
    ranges: list[tuple[int, int]] = field(default_factory=list)


def parse_hunks(diff: str) -> dict[str, FileHunks]:
    """Map each file the diff touches to its hunks' new-side line ranges.

    Paths follow `context.parse_changed_files`: the `+++ b/path` post-image
    path, so a rename is keyed by its new name; `+++ /dev/null` is a deletion,
    keyed by its `--- a/path` and marked `deleted`. A hunk adding no lines
    (`+c,0`) has no range. A hunk cut off before its counted lines end (a
    truncated diff) is dropped: GitHub's diff may not match what was cut.
    """
    files: dict[str, FileHunks] = {}
    current: FileHunks | None = None
    old_path = ""
    pending: tuple[int, int] | None = None  # the open hunk's new-side range
    old_left = new_left = 0

    # Split on "\n" only: splitlines() also breaks on \f, \v, U+2028 and friends,
    # which git treats as content, so a valid hunk would read as truncated.
    lines = diff.split("\n")
    if lines[-1] == "":
        lines.pop()
    for line in lines:
        line = line.removesuffix("\r")  # a CRLF diff parses as it did with splitlines()
        if (old_left > 0 or new_left > 0) and line[:1] not in _BODY_PREFIXES:
            # The hunk ended before its counted lines did (a truncated or
            # miscounted diff): drop it and read this line as a header, so the
            # next file or hunk is not swallowed as content.
            pending, old_left, new_left = None, 0, 0
        if old_left > 0 or new_left > 0:
            # Inside a hunk body: every prefixed line is content.
            if line.startswith("\\"):
                continue  # "\ No newline at end of file"
            if line.startswith("-"):
                old_left -= 1
            elif line.startswith("+"):
                new_left -= 1
            else:  # context; some tools strip a blank context line to ""
                old_left -= 1
                new_left -= 1
            if old_left <= 0 and new_left <= 0:
                if pending and current is not None:
                    current.ranges.append(pending)
                old_left = new_left = 0
            continue

        if line.startswith("diff --git "):
            current, old_path = None, ""
        elif line.startswith("--- "):
            old_path = gating.diff_path(line[4:], "a/")
        elif line.startswith("+++ "):
            target = gating.diff_path(line[4:], "b/")
            if target == "/dev/null":
                if not old_path:  # malformed: a deletion with no `--- a/path`
                    current = None
                    continue
                current = files.setdefault(old_path, FileHunks())
                current.deleted = True
            else:
                current = files.setdefault(target, FileHunks())
        elif current is not None and (match := _HUNK_HEADER.match(line)):
            old_count = 1 if match.group(2) is None else int(match.group(2))
            start = int(match.group(3))
            new_count = 1 if match.group(4) is None else int(match.group(4))
            pending = (start, start + new_count - 1) if new_count > 0 else None
            old_left, new_left = old_count, new_count
            if old_left == 0 and new_left == 0 and pending:
                current.ranges.append(pending)

    return files


def applyability(finding: NormalizedFinding, hunks: dict[str, FileHunks]) -> str | None:
    """`None` if the finding's fix can be an inline suggestion, else why not.

    The R7 conditions are checked in order; the first that fails names the reason.
    """
    start = finding.fix_start_line
    if finding.replacement is None or start is None:
        return NO_REPLACEMENT
    end = finding.fix_end_line if finding.fix_end_line is not None else start

    file_hunks = hunks.get(finding.file)
    if file_hunks is None:
        return NOT_IN_DIFF
    if file_hunks.deleted:
        return FILE_DELETED

    def hunk_of(line: int) -> int | None:
        for index, (low, high) in enumerate(file_hunks.ranges):
            if low <= line <= high:
                return index
        return None

    first, last = hunk_of(start), hunk_of(end)
    if first is None or last is None:
        return OUTSIDE_DIFF
    if first != last:
        return SPANS_HUNKS

    if end - start + 1 > MAX_SUGGESTION_LINES:
        return TOO_LONG
    # Same blank-text rule as report.normalize, so the two never disagree.
    if _stated(finding.previously_rejected) and not _stated(finding.new_evidence):
        return PREVIOUSLY_REJECTED
    if not finding.reported_by:
        return NO_REPORTER
    return None


# --- Review payload (R8) -----------------------------------------------------

REVIEW_MARKER = "<!-- tri-review-suggestions -->"


def _fix_range(f: NormalizedFinding) -> tuple[int, int]:
    """The fix's first and last line; callers pass applyable findings only."""
    start = f.fix_start_line or 0
    return start, f.fix_end_line if f.fix_end_line is not None else start


def fingerprint(f: NormalizedFinding) -> str:
    """16 hex characters identifying this suggestion across runs.

    Built from the R8 fields: path, fix range, replacement and title, not the
    finding's ID, so a re-run that only renumbers the finding is recognised as
    posted. The title and replacement are hashed as given, so a re-run that
    rewords the title (even whitespace only) yields a new fingerprint.
    """
    start, end = _fix_range(f)
    fields = [f.file, str(start), str(end), f.replacement or "", f.title]
    return hashlib.sha256("\0".join(fields).encode()).hexdigest()[:16]


def comment_body(f: NormalizedFinding) -> str:
    """The inline comment: the R6 block without its location line, code block
    or status line, then a suggestion block holding `replacement` verbatim."""
    block = render_block(f.model_copy(update={"replacement": None}), "", "").split("\n")
    del block[1]  # the location line: the comment sits on those lines

    lines = [f"<!-- tri-review-suggestion:{fingerprint(f)} -->"]
    if f.kind == "unique" and f.reported_by:
        lines.append(f"**Unverified:** only `{f.reported_by[0]}` reported this.")
    lines += block

    replacement = f.replacement or ""
    if replacement and not replacement.endswith("\n"):
        replacement += "\n"
    fence = fence_for(replacement)
    lines += ["", f"{fence}suggestion\n{replacement}{fence}"]
    return "\n".join(lines)


def build_review_payload(findings: list[NormalizedFinding], head_sha: str) -> dict | None:
    """The reviews API body for every applyable finding, or None if there are
    none or the head SHA is unknown (the comments must pin to a commit)."""
    applyable = [f for f in findings if f.applyable]
    if not applyable or not head_sha:
        return None

    comments = []
    for f in applyable:
        start, end = _fix_range(f)
        comment: dict = {"path": f.file}
        if start != end:
            comment |= {"start_line": start, "start_side": "RIGHT"}
        comment |= {"line": end, "side": "RIGHT", "body": comment_body(f)}
        comments.append(comment)

    count = len(applyable)
    noun = "fix" if count == 1 else "fixes"
    body = (
        f"{REVIEW_MARKER}\n**tri-review suggested changes** for `{head_sha[:7]}`: "
        f"{count} {noun} from the report. Unverified ones were reported by one model only."
    )
    return {"commit_id": head_sha, "event": "COMMENT", "body": body, "comments": comments}

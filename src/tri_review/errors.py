"""Error types that map to distinct CLI exit codes."""

from __future__ import annotations


class TriReviewError(Exception):
    """Base for every error tri-review reports without a traceback."""

    exit_code = 1


class PreflightError(TriReviewError):
    """Environment isn't ready: no gh, not authenticated, not a GitHub repo."""

    exit_code = 2


class PRNotFoundError(TriReviewError):
    """No pull request to review."""

    exit_code = 3


class InsufficientReviewsError(TriReviewError):
    """Fewer than two models returned a review, so there is nothing to triangulate."""

    exit_code = 4


class NothingToReview(Exception):
    """The PR holds nothing worth spending a review on.

    Deliberately *not* a `TriReviewError`. This is an outcome, not a failure:
    the tool looked, correctly determined there was no code to triangulate, and
    stopped before spending anything. Reporting it as an error would fail a CI
    check on a docs-only pull request, which is precisely backwards -- and it is
    what exit code 3 (`PRNotFoundError`, "no such PR") used to do here.

    `gate` names which check stopped the run. Downstream -- the Action's comment
    in particular -- needs to tell them apart: a path skip spent nothing at all,
    while a triage skip did pay for one small model call to reach its verdict.
    """

    exit_code = 0

    def __init__(self, message: str, gate: str = "path") -> None:
        super().__init__(message)
        self.gate = gate

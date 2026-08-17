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

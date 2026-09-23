"""LangGraph state shared across the fan-out and fan-in nodes."""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from .schema import ReviewResult


class ReviewState(TypedDict, total=False):
    pr_number: str
    # "owner/name" to review a repo the process is not checked out into, or
    # absent/empty to use whatever repo the cwd is in.
    repo: str
    # The PR's head commit SHA. File contents are read at this ref, so leaving it
    # to be resolved by the entry node is safe, but overriding it with anything
    # other than the reviewed revision silently corrupts the review.
    head_ref: str
    excludes: tuple[str, ...]
    # The PR diff, when the caller already fetched it (the triage gate needs it
    # before the graph starts). Present to avoid asking GitHub for the same diff
    # twice, never to substitute a different one.
    diff: str
    payload: str
    # Holds a context.ReviewContext. Typed as Any because LangGraph resolves
    # these annotations at runtime, and because the state must declare the key
    # at all -- LangGraph silently drops update keys it doesn't know about.
    context: Any
    # Dismissals recorded for the reviewed repo, read at its BASE ref so a PR
    # cannot excuse its own findings. Resolved in fetch_context_node -- before
    # any model is called -- so a malformed ledger fails the run while it is
    # still free, rather than after three reviews have been paid for.
    dismissals: list
    # operator.add makes the parallel reviewer nodes append rather than overwrite.
    results: Annotated[list[ReviewResult], operator.add]
    final_report: str

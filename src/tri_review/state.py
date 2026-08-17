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
    payload: str
    # Holds a context.ReviewContext. Typed as Any because LangGraph resolves
    # these annotations at runtime, and because the state must declare the key
    # at all -- LangGraph silently drops update keys it doesn't know about.
    context: Any
    # operator.add makes the parallel reviewer nodes append rather than overwrite.
    results: Annotated[list[ReviewResult], operator.add]
    final_report: str

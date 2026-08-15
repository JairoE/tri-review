"""LangGraph state shared across the fan-out and fan-in nodes."""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from .schema import ReviewResult


class ReviewState(TypedDict, total=False):
    pr_number: str
    payload: str
    # Holds a context.ReviewContext. Typed as Any because LangGraph resolves
    # these annotations at runtime, and because the state must declare the key
    # at all -- LangGraph silently drops update keys it doesn't know about.
    context: Any
    # operator.add makes the parallel reviewer nodes append rather than overwrite.
    results: Annotated[list[ReviewResult], operator.add]
    final_report: str

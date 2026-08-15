"""LangGraph state shared across the fan-out and fan-in nodes."""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from .schema import ReviewResult


class ReviewState(TypedDict, total=False):
    pr_number: str
    payload: str
    # operator.add makes the parallel reviewer nodes append rather than overwrite.
    results: Annotated[list[ReviewResult], operator.add]
    final_report: str

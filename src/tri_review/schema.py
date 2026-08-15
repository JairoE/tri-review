"""Structured findings.

Reviews are structured rather than free text so the synthesizer can match the
same issue across models by file and line instead of guessing from prose.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["critical", "major", "minor"]
Category = Literal["bug", "security", "performance", "logic"]


class Finding(BaseModel):
    file: str = Field(description="Repository-relative path the finding applies to.")
    line: int | None = Field(default=None, description="1-indexed line number, or null if not line-specific.")
    severity: Severity = Field(description="critical, major, or minor.")
    category: Category = Field(description="bug, security, performance, or logic.")
    title: str = Field(description="One-line statement of the problem.")
    detail: str = Field(description="What is wrong and why it matters.")
    suggested_fix: str | None = Field(default=None, description="Concrete change to make, or null.")


class ReviewOutput(BaseModel):
    """What each reviewer model is asked to return."""

    findings: list[Finding] = Field(
        default_factory=list,
        description="Every issue found. Empty if the diff is clean -- never invent findings.",
    )


class ReviewResult(BaseModel):
    """One model's outcome, success or failure. Never raises into the graph."""

    model: str
    findings: list[Finding] = Field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

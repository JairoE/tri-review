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


class ContextSummary(BaseModel):
    """What would be sent to the models, without the contents themselves.

    `ReviewContext` is a plain dataclass holding whole file bodies; this is its
    wire shape, so the confirm-before-spending step can show the size and shape
    of a review without shipping megabytes of source to the browser.
    """

    files_included: list[str] = Field(default_factory=list)
    dropped: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    rejected: list[str] = Field(default_factory=list)
    estimated_tokens: int = 0
    diff_lines: int = 0
    diff_overflow: int = 0

    @classmethod
    def of(cls, ctx) -> "ContextSummary":
        """Build a summary from a context.ReviewContext."""
        return cls(
            files_included=list(ctx.files),
            dropped=list(ctx.dropped),
            missing=list(ctx.missing),
            rejected=list(ctx.rejected),
            estimated_tokens=ctx.estimated_tokens,
            diff_lines=len(ctx.diff.splitlines()),
            diff_overflow=ctx.diff_overflow,
        )


SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2}


def render_findings_md(result: ReviewResult) -> str:
    """Render one model's review as a standalone Markdown document.

    The synthesizer produces prose about all three reviews together; this is the
    other half -- what a single model said, on its own, so three of them can be
    read side by side and compared.
    """
    if not result.ok:
        return (
            f"> **{result.model} did not report.**\n>\n"
            f"> ```\n> {result.error}\n> ```\n"
        )

    if not result.findings:
        return f"_{result.model} found no issues in this diff._\n"

    ordered = sorted(
        result.findings,
        key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.file, f.line or 0),
    )

    parts: list[str] = []
    count = len(ordered)
    parts.append(f"**{count} finding{'' if count == 1 else 's'}**\n")

    for finding in ordered:
        location = finding.file + (f":{finding.line}" if finding.line else "")
        parts.append(
            f"### {finding.severity.capitalize()} · {finding.category} — {finding.title}\n\n"
            f"`{location}`\n\n{finding.detail}"
            + (f"\n\n**Suggested fix**\n\n{finding.suggested_fix}" if finding.suggested_fix else "")
        )

    return "\n\n".join(parts) + "\n"

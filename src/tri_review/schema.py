"""Structured findings.

Reviews are structured rather than free text so the synthesizer can match the
same issue across models by file and line instead of guessing from prose.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, ValidationError, model_validator
from pydantic.json_schema import SkipJsonSchema

Severity = Literal["critical", "major", "minor"]
Category = Literal["bug", "security", "performance", "logic"]


class Finding(BaseModel):
    file: str = Field(description="Repository-relative path the finding applies to.")
    line: int | None = Field(default=None, description="1-indexed line number, or null if not line-specific.")
    severity: Severity = Field(description="critical, major, or minor.")
    category: Category = Field(description="bug, security, performance, or logic.")
    title: str = Field(description="One-line statement of the problem.")
    detail: str = Field(
        description="The problem: what is wrong and its consequence, in at most two sentences."
    )
    # No default: the model is shown this as required. A reviewer that omits it
    # anyway is caught by `_normalize_fix`, which keeps the finding with "".
    trigger: str = Field(
        description=(
            "One sentence: the input, state or sequence of events that makes the problem happen."
        )
    )
    suggested_fix: str | None = Field(
        default=None,
        description="The fix: one sentence saying what to change, or null.",
    )
    fix_start_line: int | None = Field(
        default=None,
        description=(
            "First line the fix replaces, 1-indexed, in the PR head's version of `file`. "
            "Null when the fix is not a local code change."
        ),
    )
    fix_end_line: int | None = Field(
        default=None,
        description="Last line the fix replaces, inclusive. Defaults to fix_start_line.",
    )
    replacement: str | None = Field(
        default=None,
        description=(
            "Exact code that replaces fix_start_line..fix_end_line, with its indentation and "
            "no line-number prefixes. An empty string deletes those lines. Null when the fix "
            "is not a local code change."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _title_from_detail(cls, data):
        """Fill a missing title from the detail rather than reject the finding.

        Still required in the schema the model is shown -- a before-validator
        does not change it -- so this only catches a reviewer that ignores the
        schema. claude-sonnet-5 did, live: two findings with no `title` and a
        full `detail`, and the whole review was discarded over a one-line
        heading the detail already contained.
        """
        if not isinstance(data, dict) or str(data.get("title") or "").strip():
            return data
        detail = data.get("detail")
        if not isinstance(detail, str) or not detail.strip():
            return data
        return {**data, "title": _headline(detail)}

    @model_validator(mode="before")
    @classmethod
    def _normalize_fix(cls, data):
        """Keep the finding when its trigger or fix fields are unusable.

        A missing or blank `trigger` becomes "". `fix_end_line` defaults to the
        start line. A fix that cannot be located (start below 1, end before
        start, or a replacement with no start line) loses all three fix fields
        rather than the whole finding, which is still a valid report.
        """
        if not isinstance(data, dict):
            return data
        data = dict(data)
        trigger = data.get("trigger")
        data["trigger"] = trigger.strip() if isinstance(trigger, str) else ""

        start, end = data.get("fix_start_line"), data.get("fix_end_line")
        if start is not None and end is None and _is_int(start):
            end = data["fix_end_line"] = start
        unlocatable = (
            (_is_int(start) and start < 1)
            or (_is_int(start) and _is_int(end) and end < start)
            or (start is None and data.get("replacement") is not None)
        )
        if unlocatable:
            data["fix_start_line"] = data["fix_end_line"] = data["replacement"] = None
        return data


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


_TITLE_LIMIT = 100


def _headline(text: str) -> str:
    """The first sentence of `text`, on one line and at most _TITLE_LIMIT chars."""
    line = text.strip().splitlines()[0].strip()
    sentence = line.split(". ", 1)[0].rstrip(".")
    if len(sentence) <= _TITLE_LIMIT:
        return sentence
    return sentence[: _TITLE_LIMIT - 1].rstrip() + "…"


class ReviewOutput(BaseModel):
    """What each reviewer model is asked to return."""

    findings: list[Finding] = Field(
        default_factory=list,
        description="Every issue found. Empty if the diff is clean -- never invent findings.",
    )
    # Filled by `_set_aside_malformed`, never by the model: SkipJsonSchema keeps
    # it out of the schema providers are shown, and out of the cache key built
    # from that schema.
    malformed: SkipJsonSchema[list[str]] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _set_aside_malformed(cls, data):
        """Validate findings one at a time, so one bad entry costs only itself.

        Without this, a single finding that misses the schema fails the whole
        ReviewOutput, and every valid finding beside it is thrown away with it
        -- the reviewer then counts as having not reported at all. What does
        not validate is recorded in `malformed` so it is reported, not hidden.
        """
        if not isinstance(data, dict) or not isinstance(data.get("findings"), list):
            return data
        kept: list[Finding] = []
        malformed: list[str] = []
        for index, item in enumerate(data["findings"]):
            try:
                kept.append(Finding.model_validate(item))
            except ValidationError as exc:
                malformed.append(_describe_malformed(index, item, exc))
        return {**data, "findings": kept, "malformed": malformed}


def _describe_malformed(index: int, item, exc: ValidationError) -> str:
    """One line naming which finding failed and on what, without echoing its body."""
    errors = exc.errors()
    first = errors[0]
    where = ".".join(str(part) for part in first["loc"]) or "finding"
    file = item.get("file") if isinstance(item, dict) else None
    label = f"findings[{index}]" + (f" ({file})" if isinstance(file, str) and file else "")
    more = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
    return f"{label}: {where}: {first['msg']}{more}"


class ReviewResult(BaseModel):
    """One model's outcome, success or failure. Never raises into the graph."""

    model: str
    findings: list[Finding] = Field(default_factory=list)
    error: str | None = None
    # Set when this came from the on-disk cache rather than a provider call.
    # Reported to the user because "3 models agreed" reads differently once you
    # know two of them last ran an hour ago against the same bytes.
    cached: bool = False
    # Set when the model returned zero findings after spending nearly its whole
    # output budget on internal reasoning -- a schema-valid but low-confidence
    # empty answer, indistinguishable from a genuine clean pass unless flagged.
    low_confidence_reason: str | None = None
    # Findings this model returned that did not match the schema and were set
    # aside, one line each (see ReviewOutput._set_aside_malformed).
    malformed_findings: list[str] = Field(default_factory=list)
    # Provider-side prompt cache usage for this call, where the provider
    # reports it (Anthropic does). None means no signal, not zero. The only
    # ground truth that caching is working: a miss still succeeds, it just
    # costs more.
    prompt_cache_read_tokens: int | None = None
    prompt_cache_write_tokens: int | None = None

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

    set_aside = ""
    if result.malformed_findings:
        count = len(result.malformed_findings)
        set_aside = (
            f"\n_{count} malformed finding{'' if count == 1 else 's'} set aside: "
            + "; ".join(result.malformed_findings)
            + "_\n"
        )

    if not result.findings:
        if result.low_confidence_reason:
            return (
                f"_{result.model} returned 0 findings but is flagged low-confidence: "
                f"{result.low_confidence_reason}. Treat as inconclusive, not a clean pass._\n"
                + set_aside
            )
        return f"_{result.model} found no issues in this diff._\n" + set_aside

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

    return "\n\n".join(parts) + "\n" + set_aside

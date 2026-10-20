"""The synthesized report, built in code from structured findings.

The synthesizer's tags and reporter list are claims; `normalize` checks them
against who actually reported in this run and enforces the tagging rules
(R4), then orders and numbers the findings (R5). Rendering (R6, R11) is string building over the normalized list. Pure
functions: no LLM, no I/O.
"""

from __future__ import annotations

import posixpath
from urllib.parse import quote

from tri_review.schema import SEVERITY_ORDER, NormalizedFinding, ReportFinding

ACTION_ORDER = {"blocking": 0, "blocking_pending_check": 1, "optional": 2}

SYNTHESIZER_ONLY = "suggested by the synthesizer, not reported by any reviewer"
PREVIOUSLY_REJECTED = "previously rejected, no new evidence"
SINGLE_MODEL = "only one model reported this"
MINOR_SEVERITY = "severity is minor"
NO_FALSIFIER = "no falsifier named"
NO_CHECK = "no check named"


def normalize(findings: list[ReportFinding], reported: list[str]) -> list[NormalizedFinding]:
    """Enforce R4 on each finding, then sort and number them F1..Fn (R5).

    `reported` is the model specs that returned a successful review this run.
    """
    panel = set(reported)
    staged: list[NormalizedFinding] = []
    for finding in findings:
        reporters = list(dict.fromkeys(m for m in finding.reported_by if m in panel))
        kind = (
            "consensus" if len(reporters) >= 2 else "unique" if reporters else "synthesizer"
        )
        reason = _downgrade_reason(finding, kind)
        staged.append(
            NormalizedFinding.model_validate(
                {
                    **finding.model_dump(),
                    "reported_by": reporters,
                    "action": "optional" if reason else finding.action,
                    "id": "",
                    "kind": kind,
                    "downgrade_reason": reason,
                }
            )
        )

    staged.sort(
        key=lambda f: (
            ACTION_ORDER[f.action],
            SEVERITY_ORDER[f.severity],
            f.file,
            f.line is None,
            f.line or 0,
        )
    )
    return [f.model_copy(update={"id": f"F{n}"}) for n, f in enumerate(staged, start=1)]


def _downgrade_reason(finding: ReportFinding, kind: str) -> str | None:
    """Why a Blocking tag does not hold (R4 rules 3-6), or None if it does.

    Optional findings are never downgraded, so they never get a reason. When
    several rules apply, the first one below names the reason: synthesizer
    suggestion, previously rejected, single model, minor, no falsifier, no
    check. Any of them makes the finding optional, so only the wording changes.
    """
    if finding.action == "optional":
        return None
    if kind == "synthesizer":
        return SYNTHESIZER_ONLY
    if _stated(finding.previously_rejected) and not _stated(finding.new_evidence):
        return PREVIOUSLY_REJECTED
    if kind != "consensus":
        return SINGLE_MODEL
    if finding.severity == "minor":
        return MINOR_SEVERITY
    if not _stated(finding.falsified_by):
        return NO_FALSIFIER
    if finding.action == "blocking_pending_check" and not _stated(finding.check):
        return NO_CHECK
    return None


def _stated(text: str | None) -> bool:
    return bool(text and text.strip())


# --- Rendering (R6, R11) -----------------------------------------------------

ACTION_LABEL = {
    "blocking": "Blocking",
    "blocking_pending_check": "Blocking pending check",
    "optional": "Optional",
}

UNVERIFIED = "(unverified by the other reviewers)"

_LANGUAGES = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "jsx",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".swift": "swift",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".sh": "bash",
    ".bash": "bash",
    ".sql": "sql",
    ".json": "json",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".toml": "toml",
    ".html": "html",
    ".css": "css",
    ".md": "markdown",
}


def language_for(path: str) -> str:
    """The code-fence language for `path`'s extension, or "" if unknown."""
    return _LANGUAGES.get(posixpath.splitext(path)[1].lower(), "")


def fence_for(text: str) -> str:
    """A backtick fence that `text` cannot close early (R7 fence rule).

    One backtick longer than the longest run that starts a line of `text`,
    never fewer than three. Leading whitespace is ignored when looking for
    that run: CommonMark lets a closing fence be indented, so an indented
    run would end a shorter fence just the same.
    """
    longest = 0
    for line in text.splitlines():
        stripped = line.lstrip()
        longest = max(longest, len(stripped) - len(stripped.lstrip("`")))
    return "`" * max(3, longest + 1)


def render_block(f: NormalizedFinding, link_repo: str, head_sha: str, *, with_id: bool = True) -> str:
    """One finding as a Problem / Trigger / Fix block (R6).

    `with_id=False` is the standalone form used by the per-model view and the
    raw fallback (R11): no ID, no reporter attribution (the caller's heading
    says whose finding it is), and no inline-suggestion status line.
    """
    heading = f"{f.id} · " if with_id else ""
    lines = [f"### {heading}**{f.severity}** · {f.category} — {_one_line(f.title)}"]

    location = _location_link(f, link_repo, head_sha)
    if with_id:
        location += f" · {_reporters(f)}"
    lines.append(location)

    if f.downgrade_reason:
        lines.append(f"_Optional — {f.downgrade_reason}_")
    if _stated(f.previously_rejected):
        lines.append(f"_Previously rejected: {_one_line(f.previously_rejected)}_")
        if _stated(f.new_evidence):
            lines.append(f"_New evidence: {_one_line(f.new_evidence)}_")

    lines.append("")
    lines.append(f"**Problem:** {_one_line(f.problem)}")
    lines.append(f"**Trigger:** {_one_line(f.trigger) if _stated(f.trigger) else '_not stated_'}")
    if _stated(f.fix):
        lines.append(f"**Fix:** {_one_line(f.fix)}")

    if f.replacement is not None:
        fence = fence_for(f.replacement)
        lines += ["", f"{fence}{language_for(f.file)}", f.replacement, fence]
        if with_id:
            status = _inline_status(f)
            if status:
                lines += ["", status]
    return "\n".join(lines)


def render_report(findings: list[NormalizedFinding], link_repo: str, head_sha: str) -> str:
    """The three report sections: Consensus Findings, Unique Insights, and
    Actionable Next Steps (R6). The header notes are the caller's."""
    consensus = [f for f in findings if f.kind == "consensus"]
    unique = [f for f in findings if f.kind != "consensus"]  # unique + synthesizer
    steps = [_next_step(f) for f in findings if _stated(f.fix)]

    def section(title: str, body: list[str], empty: str) -> str:
        return f"## {title}\n\n" + ("\n\n".join(body) if body else f"_{empty}_") + "\n"

    return "\n".join(
        [
            section(
                "Consensus Findings",
                [render_block(f, link_repo, head_sha) for f in consensus],
                "No consensus findings.",
            ),
            section(
                "Unique Insights",
                [render_block(f, link_repo, head_sha) for f in unique],
                "No unique insights.",
            ),
            section("Actionable Next Steps", ["\n".join(steps)] if steps else [], "No actionable next steps."),
        ]
    )


def _next_step(f: NormalizedFinding) -> str:
    text = f"- **{ACTION_LABEL[f.action]}** {f.id} · `{_location_text(f)}` — {_sentence(f.fix)}"
    if f.action == "blocking_pending_check" and _stated(f.check):
        text += f" Check: {_sentence(f.check)}"
    if f.action != "optional" and _stated(f.falsified_by):
        text += f" Falsified by: {_sentence(f.falsified_by)}"
    return text


def _location_range(f: NormalizedFinding) -> tuple[int, int] | None:
    """The lines the finding points at: the fix range if any, else its line."""
    if f.fix_start_line is not None:
        end = f.fix_end_line if f.fix_end_line is not None else f.fix_start_line
        return f.fix_start_line, end
    if f.line is not None:
        return f.line, f.line
    return None


def _location_text(f: NormalizedFinding) -> str:
    span = _location_range(f)
    if span is None:
        return f.file
    start, end = span
    return f"{f.file}:{start}" if start == end else f"{f.file}:{start}-{end}"


def _location_link(f: NormalizedFinding, link_repo: str, head_sha: str) -> str:
    text = f"`{_location_text(f)}`"
    if not (link_repo and head_sha):
        return text
    url = f"https://github.com/{link_repo}/blob/{head_sha}/{quote(f.file, safe='/')}"
    span = _location_range(f)
    if span is not None:
        start, end = span
        url += f"#L{start}" if start == end else f"#L{start}-L{end}"
    return f"[{text}]({url})"


def _reporters(f: NormalizedFinding) -> str:
    if f.kind == "synthesizer":
        return SYNTHESIZER_ONLY
    if f.kind == "unique":
        return f"**`{f.reported_by[0]}`** {UNVERIFIED}"
    return ", ".join(f"`{m}`" for m in f.reported_by)


def _inline_status(f: NormalizedFinding) -> str:
    """The suggestion status line (R6); empty when there is nothing to say."""
    if f.applyable:
        start, end = f.fix_start_line, f.fix_end_line
        if start is None:
            return ""
        end = start if end is None else end
        lines = f"L{start}" if start == end else f"L{start}-{end}"
        return f"_Inline suggestion posted on {lines}._"
    if f.inline_reason:
        return f"_No inline suggestion: {f.inline_reason}._"
    return ""


def _one_line(text: str) -> str:
    """Collapse whitespace so a model's stray newline cannot break the layout."""
    return " ".join(text.split())


def _sentence(text: str | None) -> str:
    """`text` on one line, ending in a full stop (not doubled if it has one)."""
    line = _one_line(text or "")
    return line if line.endswith((".", "!", "?")) else line + "."

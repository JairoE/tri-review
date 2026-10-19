"""The synthesized report, built in code from structured findings.

The synthesizer's tags and reporter list are claims; `normalize` checks them
against who actually reported in this run and enforces the tagging rules
(R4), then orders and numbers the findings (R5). Pure functions: no LLM, no I/O.
"""

from __future__ import annotations

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

    Optional findings are never downgraded, so they never get a reason.
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

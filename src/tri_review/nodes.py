"""Graph nodes: three reviewers that never raise, and a synthesizer that reads them."""

from __future__ import annotations

import json

from langchain_core.messages import HumanMessage, SystemMessage

from . import config
from .errors import InsufficientReviewsError
from .providers import build_llm, provider_of
from .schema import ReviewOutput, ReviewResult
from .state import ReviewState

REVIEW_PROMPT = """You are an expert code reviewer examining a pull request.

Focus on bugs, security vulnerabilities, logic errors, and performance problems.
Ignore pure style and formatting nitpicks -- a linter handles those.

You are given the PR diff and, where available, the full current contents of the
files it touches. Review the change, not the whole file: only report issues the
diff introduces or leaves unaddressed in the code it touches.

Report every real issue you find with its file and line. If the diff is clean,
return an empty findings list. Do not invent findings to appear thorough -- a
false positive costs the reader more than a missed nitpick.
"""

SYNTHESIS_PROMPT = """You are the lead engineer synthesizing several independent code reviews.

You receive structured findings from multiple AI reviewers who did not see each
other's work. Findings that describe the same underlying issue are corroborated
and high-trust; findings only one model reported are unverified.

Produce a Markdown report with exactly these three sections:

## Consensus Findings
Issues reported by 2 or more models. Match on the underlying problem, not on
wording -- the same bug described differently in the same file and region is one
finding. State each once, with file and line, and note which models found it.

## Unique Insights
Issues reported by only one model. Name that model in bold at the very start of
the bullet, then the "unverified" qualifier, e.g. "**gemini-3.7-flash**
(unverified by the other reviewers):" -- never write "unverified by the other
reviewer(s)" without naming the reporting model in that same phrase. A reader
must be able to tell who found the issue from the bullet itself, without
needing a separate line below it.

## Actionable Next Steps
The specific changes worth making, highest value first, with file and line
references and concrete code where useful. If a step comes from a Unique
Insight rather than a Consensus Finding, name the reporting model here too --
do not let a single-model, unverified claim read as equally established just
because it reached this section.

If a section has nothing in it, say so in one line rather than padding it.
Do not reproduce the raw reviews.
"""


def review_with(model_name: str, payload: str, llm_builder=build_llm) -> ReviewResult:
    """Run one reviewer. Returns a ReviewResult on success or failure — never raises."""
    try:
        llm = llm_builder(model_name)
        structured = llm.with_structured_output(ReviewOutput)
        output = structured.invoke(
            [SystemMessage(content=REVIEW_PROMPT), HumanMessage(content=payload)]
        )
        findings = output.findings if output is not None else []
        return ReviewResult(model=model_name, findings=findings)
    except Exception as exc:  # noqa: BLE001 - one flaky provider must not abort the run
        return ReviewResult(model=model_name, error=_describe_error(exc))


def _describe_error(exc: Exception) -> str:
    """Format a reviewer failure, including the real cause behind a chained exception.

    Connection-layer SDK errors (e.g. anthropic.APIConnectionError) deliberately
    keep their `str()` generic -- "Connection error." every time -- while the
    actual httpx/OS-level failure (DNS lookup, TCP reset, TLS handshake, a
    timed-out socket) lives on `__cause__` and would otherwise be discarded
    here. Surfacing it is the difference between "connection error, cause
    unknown" and "connection error: [Errno -2] Name or service not known" the
    next time this happens in CI.
    """
    description = f"{type(exc).__name__}: {exc}"
    if exc.__cause__ is not None:
        description += f" (caused by {exc.__cause__!r})"
    return description


def make_review_node(model_name: str, llm_builder=build_llm):
    """Build a LangGraph node that appends exactly one ReviewResult to state."""

    def node(state: ReviewState) -> dict:
        return {"results": [review_with(model_name, state["payload"], llm_builder)]}

    return node


def synthesize_node(state: ReviewState, llm_builder=build_llm) -> dict:
    """Cross-reference the reviews. Requires at least two successful ones."""
    results = state.get("results", [])
    succeeded = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]

    if len(succeeded) < 2:
        detail = "; ".join(f"{r.model}: {r.error}" for r in failed) or "no models ran"
        raise InsufficientReviewsError(
            f"Only {len(succeeded)} of {len(results)} models returned a review, so there is "
            f"nothing to triangulate.\nFailures: {detail}"
        )

    return {"final_report": _synthesize(succeeded, failed, llm_builder)}


def _synthesize(succeeded, failed, llm_builder) -> str:
    """Ask a model to cross-reference the structured findings into one report."""
    payload = json.dumps(
        [
            {"model": r.model, "findings": [f.model_dump() for f in r.findings]}
            for r in succeeded
        ],
        indent=2,
    )
    header = _failure_note(failed) + _diversity_note(succeeded)

    try:
        llm = llm_builder(config.synthesizer_model())
        response = llm.invoke(
            [SystemMessage(content=SYNTHESIS_PROMPT), HumanMessage(content=payload)]
        )
        return header + _text_of(response)
    except Exception as exc:  # noqa: BLE001 - the reviews already cost money; don't lose them
        return (
            header
            + f"> Synthesis failed ({type(exc).__name__}: {exc}). "
            "Raw findings from each model follow.\n\n"
            + _raw_listing(succeeded)
        )


def _diversity_note(succeeded) -> str:
    """Warn when the surviving reviewers all came from one provider.

    The product's premise is that independent models rarely hallucinate the same
    thing. Two checkpoints of one family are not independent -- they share
    training data and failure modes -- so their agreement must not be presented
    as though it were corroboration. Computed from the models that actually
    reported, since a failed reviewer can collapse a diverse panel into a
    single-provider one.
    """
    providers = {provider_of(r.model) for r in succeeded}
    if len(providers) > 1 or None in providers:
        return ""
    return (
        f"> **All {len(succeeded)} reviewers are `{providers.pop()}` models.** Models from one "
        "provider share training data and failure modes, so agreement between them is much "
        "weaker evidence than cross-provider consensus. Read the sections below as one "
        "opinion stated repeatedly, not as independent corroboration.\n\n"
    )


def _failure_note(failed) -> str:
    if not failed:
        return ""
    lines = "\n".join(f"- `{r.model}`: {r.error}" for r in failed)
    return f"> **{len(failed)} model(s) did not report.** This review is based on the rest.\n{lines}\n\n"


def _text_of(response) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, list):  # some providers return content blocks
        return "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
    return str(content)


def _raw_listing(succeeded) -> str:
    lines: list[str] = []
    for result in succeeded:
        lines.append(f"### {result.model} ({len(result.findings)} findings)")
        for finding in result.findings:
            lines.append(
                f"- **{finding.severity}/{finding.category}** `{finding.file}:{finding.line}` "
                f"— {finding.title}\n  {finding.detail}"
            )
        lines.append("")
    return "\n".join(lines)

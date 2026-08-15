"""Graph nodes: three reviewers that never raise, and a synthesizer that reads them."""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from . import config
from .errors import InsufficientReviewsError
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
Issues reported by only one model, each attributed to that model and labeled as
unverified by the others.

## Actionable Next Steps
The specific changes worth making, highest value first, with file and line
references and concrete code where useful.

If a section has nothing in it, say so in one line rather than padding it.
Do not reproduce the raw reviews.
"""


def build_llm(model_name: str):
    """Construct a chat model from its ID, choosing the provider by ID prefix.

    No temperature is set anywhere: the current OpenAI and Anthropic flagships
    reject sampling parameters outright.
    """
    timeout = config.model_timeout()
    if model_name.startswith(("gpt-", "o1", "o3", "o4")):
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=model_name, timeout=timeout, max_retries=1)
    if model_name.startswith("claude-"):
        from langchain_anthropic import ChatAnthropic

        # Generous max_tokens: on current Anthropic models max_tokens caps
        # thinking plus response together, so a tight value truncates output.
        return ChatAnthropic(model=model_name, timeout=timeout, max_retries=1, max_tokens=8000)
    if model_name.startswith("gemini"):
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(model=model_name, timeout=timeout, max_retries=1)
    raise ValueError(f"Unrecognized model ID {model_name!r} — cannot pick a provider.")


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
        return ReviewResult(model=model_name, error=f"{type(exc).__name__}: {exc}")


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
    """Placeholder: lists raw findings. Replaced by the LLM synthesizer in task 6."""
    lines = [f"Reviews from: {', '.join(r.model for r in succeeded)}"]
    for result in succeeded:
        lines.append(f"\n### {result.model} ({len(result.findings)} findings)")
        for finding in result.findings:
            lines.append(f"- {finding.file}:{finding.line} {finding.title}")
    for result in failed:
        lines.append(f"\n(!) {result.model} failed: {result.error}")
    return "\n".join(lines)

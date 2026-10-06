"""Graph nodes: three reviewers that never raise, and a synthesizer that reads them."""

from __future__ import annotations

import json

from langchain_core.messages import HumanMessage, SystemMessage

from . import cache, config, dismissals
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

Report every real issue you find with its file and line. Be specific and
concrete. An empty findings list is a strong claim that the change is
correct -- only make it if you have actually checked each changed hunk and
found nothing wrong.
"""

SYNTHESIS_PROMPT = """You are the lead engineer synthesizing several independent code reviews.

You receive structured findings from multiple AI reviewers who did not see each
other's work. Findings that describe the same underlying issue are corroborated
and high-trust; findings only one model reported are unverified.

A reviewer entry may carry a non-null `low_confidence_reason`. This means that
reviewer's empty findings list is inconclusive, not a verified clean pass --
treat it as though that reviewer did not weigh in at all. Never cite a
low-confidence reviewer's silence as corroboration that the diff is clean, and
never count it as a second independent "no issues found" opinion.

Produce a Markdown report with exactly these three sections:

## Consensus Findings
Issues reported by 2 or more models. Match on the underlying problem, not on
wording -- the same bug described differently in the same file and region is one
finding. State each once, with file and line, and note which models found it.
Give its severity in bold -- **critical**, **major**, or **minor** -- taking
the highest severity any of the agreeing models reported.

## Unique Insights
Issues reported by only one model. Name that model in bold at the very start of
the bullet, then the "unverified" qualifier, then that model's reported
severity in bold, e.g. "**gemini-3.7-flash** (unverified by the other
reviewers): **major** --" -- never write "unverified by the other reviewer(s)"
without naming the reporting model in that same phrase. A reader must be able
to tell who found the issue, and how serious its reporter judged it, from the
bullet itself, without needing a separate line below it.

Every bullet in both sections carries its severity label. Never omit it, even
when every finding in the section shares the same severity.

## Actionable Next Steps
The specific changes worth making, highest value first, with file and line
references and concrete code where useful. If a step comes from a Unique
Insight rather than a Consensus Finding, name the reporting model here too --
do not let a single-model, unverified claim read as equally established just
because it reached this section.

Tag every step **Blocking**, **Blocking pending check**, or **Optional** at the
start of the bullet, before any other text.

**Blocking** requires all three: it traces back to a Consensus Finding, that
finding is critical or major severity, and it is settled by what the diff and
the file contents in front of you actually show. Agreement alone is not
evidence. Reviewers who cannot observe something can still agree about it, and
where they share a wrong assumption their agreement reproduces it rather than
correcting it -- so treat consensus as a reason to look closely, not as proof.

**Blocking pending check** is for a step that would be Blocking except that it
rests on behaviour none of the reviewers could observe: how a provider, API,
library, runtime, or model actually responds; what a service accepts or
returns; what a particular version does. Name the specific check that would
settle it -- the call to make, the command to run, the page to read -- so the
reader can resolve it in one step. Never present an unobserved claim as a
required change.

Everything else -- minor-severity findings, single-model Unique Insights, and
any suggestion you are adding yourself rather than lifting from a specific
finding -- is **Optional**.

End every Blocking and Blocking pending check step with a short "Falsified by:"
clause naming what would show it to be wrong. A step whose falsifier you cannot
name is a speculation, not a requirement; make it Optional.

A reader skimming just the bold tags should be able to tell what has to be
fixed before merge, what needs one check first, and what can wait.

You may be given a list of claims a human has already reviewed and rejected on
this codebase. Never silently drop a finding because it matches one. Report it,
say plainly that it was previously rejected and give the recorded reason, and
make it Optional -- unless this diff carries new evidence the earlier rejection
did not account for, in which case say what that new evidence is. A reader must
always be able to see that a finding was raised and on what grounds it was set
aside.

If a section has nothing in it, say so in one line rather than padding it.
Do not reproduce the raw reviews.
"""


def _output_fingerprint() -> str:
    """A stable rendering of the schema reviewers are held to.

    Part of the cache key: widening `Finding` changes what a reviewer can say,
    so entries written against the old shape must not be replayed against the
    new one.
    """
    return json.dumps(ReviewOutput.model_json_schema(), sort_keys=True)


def review_with(
    model_name: str, payload: str, llm_builder=build_llm, use_cache: bool = True
) -> ReviewResult:
    """Run one reviewer. Returns a ReviewResult on success or failure — never raises.

    A cache hit here is exact: same model, same payload, same prompt, same output
    schema. That matters most on a retry after a provider flakes -- the reviews
    that already landed are not bought a second time, only the one that failed.
    """
    key = (
        cache.key_for(model_name, payload, REVIEW_PROMPT, _output_fingerprint())
        if use_cache
        else None
    )
    if key is not None:
        hit = cache.load(key)
        if hit is not None:
            hit.cached = True
            return hit

    try:
        llm = llm_builder(model_name)
        structured = llm.with_structured_output(ReviewOutput, include_raw=True)
        raw_result = structured.invoke(
            [SystemMessage(content=REVIEW_PROMPT), HumanMessage(content=payload)]
        )
        parsing_error = raw_result.get("parsing_error")
        if parsing_error is not None:
            # include_raw=True makes a parse failure land here instead of
            # raising -- without this check it is indistinguishable from a
            # genuine empty review and gets cached as one.
            raise parsing_error
        output = raw_result["parsed"]
        findings = output.findings if output is not None else []
        malformed = output.malformed if output is not None else []
        if malformed and not findings:
            # Nothing usable came back, so this is a failure, not an empty
            # review: it must not count toward the two reviews synthesis
            # needs, and -- returned before the cache write, like any failure
            # -- a retry asks this model again rather than replaying it.
            return ReviewResult(
                model=model_name,
                error=_all_malformed_error(malformed),
                malformed_findings=malformed,
            )
        result = ReviewResult(
            model=model_name,
            findings=findings,
            malformed_findings=malformed,
            low_confidence_reason=_low_confidence_reason(raw_result["raw"], findings),
        )
    except Exception as exc:  # noqa: BLE001 - one flaky provider must not abort the run
        # Deliberately not cached. Storing a failure would make the retry this
        # cache exists to cheapen return the same failure for free.
        return ReviewResult(model=model_name, error=_describe_error(exc))

    if key is not None:
        cache.save(key, result)
    return result


# Calibrated on 74 live gemini-3.7-flash calls, then re-checked against the
# current default. Every empty result observed -- across payloads of 20K and
# 110K tokens, both models, and every thinking_level -- landed at exactly 9
# answer tokens, the width of `{"findings": []}`. Runs that did report used
# 254-290 on 3.7-flash and 316-647 on 3.8-flash, so the margin either side of
# the 20-token floor got wider with the model change, not narrower. Reasoning
# on empty results ranged 819-30,588, so that floor only excludes a model that
# barely thought at all.
#
# One shape these thresholds deliberately do not flag: 3.8-flash sometimes
# answers with reasoning=0 (snap judgement, no deliberation), which the
# reasoning<=0 guard below drops before the floor is ever consulted. Every
# such run observed did report a finding, and inferring low confidence from
# *absent* reasoning data would fire on any provider that simply doesn't
# report it. Revisit if an empty result with zero reasoning is ever seen.
_MIN_ANSWER_TOKENS = 20
_MIN_REASONING_TOKENS_FOR_CONCERN = 500

def _low_confidence_reason(raw_message, findings: list) -> str | None:
    """Flag an empty result that may be an under-review rather than a clean pass.

    Named for what it looked like from the token split -- nearly all output
    spent on reasoning, a 9-token answer -- but that reading was wrong, and the
    live evidence is worth recording so nobody re-derives it. The model is not
    running out of anything: finish_reason is STOP on every observed sample,
    which is what actually rules truncation out. Its thought summary (via
    include_thoughts) reads as a survey that ends by approving the diff --
    though a summary is not the raw chain of thought, so it is evidence about
    how the model concluded, not proof that every hunk was examined. The
    9-token answer is simply the fixed width of `{"findings": []}`, and the
    reasoning count is just how long it deliberated. So neither number means
    exhaustion; what they jointly identify is "deliberated, then reported
    nothing", which on this model is unreliable often enough to be worth
    surfacing -- it was measured returning empty on a diff where the other two
    reviewers found 3 and 2 real findings.

    The reasoning floor only excludes a model that barely engaged at all. Only
    applies to empty findings: a real finding means the model did the work.
    """
    if findings:
        return None
    usage = getattr(raw_message, "usage_metadata", None)
    if not isinstance(usage, dict):
        # Absent or a provider-specific shape LangChain didn't normalize --
        # either way, no signal to act on. A malformed shape here must not
        # turn an otherwise-successful empty review into a reported failure.
        return None
    details = usage.get("output_token_details")
    reasoning = details.get("reasoning", 0) if isinstance(details, dict) else 0
    output = usage.get("output_tokens", 0)
    if not isinstance(reasoning, int) or not isinstance(output, int):
        return None
    if reasoning <= 0 or output <= 0 or reasoning > output:
        return None
    if reasoning < _MIN_REASONING_TOKENS_FOR_CONCERN:
        return None
    if output - reasoning <= _MIN_ANSWER_TOKENS:
        return (
            f"reported no findings after spending {reasoning}/{output} output tokens "
            "on internal reasoning -- this model produces that exact shape both when a "
            "diff is genuinely clean and when it has under-reviewed one, so treat it as "
            "inconclusive rather than as a verified clean pass"
        )
    return None


def _all_malformed_error(malformed: list[str]) -> str:
    """The error for a review whose every finding was set aside as malformed."""
    count = len(malformed)
    return (
        f"returned {count} finding{'' if count == 1 else 's'}, none matching the "
        "findings schema, so nothing it reported could be used: " + "; ".join(malformed)
    )


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


def make_review_node(model_name: str, llm_builder=build_llm, use_cache: bool = True):
    """Build a LangGraph node that appends exactly one ReviewResult to state."""

    def node(state: ReviewState) -> dict:
        return {
            "results": [
                review_with(model_name, state["payload"], llm_builder, use_cache)
            ]
        }

    return node


def synthesize_node(
    state: ReviewState, llm_builder=build_llm, synthesizer: str | None = None
) -> dict:
    """Cross-reference the reviews. Requires two successful ones, or one of one.

    A panel of one was chosen that way, so its single review is the whole run
    and is reported, labelled as uncorroborated. A larger panel that degrades
    to one is a failed run: it was meant to triangulate and could not, and the
    Action's fail-on-insufficient-reviews exists to say so.

    `synthesizer` is any model spec, reviewer or not. The graph always passes
    one; the fallback is for direct callers and mirrors the CLI's default.
    """
    results = state.get("results", [])
    succeeded = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]

    # One graph node per panel member, each appending exactly one result, so
    # len(results) is the panel size. Stated as a rule rather than min(), so
    # an empty panel reads as "needs two" instead of hiding in arithmetic.
    required = 1 if len(results) == 1 else 2
    if len(succeeded) < required:
        detail = "; ".join(f"{r.model}: {r.error}" for r in failed) or "no models ran"
        raise InsufficientReviewsError(
            f"Only {len(succeeded)} of {len(results)} models returned a review, so there is "
            f"nothing to triangulate.\nFailures: {detail}"
        )

    if synthesizer is None:
        synthesizer = config.synthesizer_model() or results[0].model

    report, synthesized = _synthesize(
        succeeded, failed, llm_builder, state.get("dismissals") or [], synthesizer
    )
    return {"final_report": report, "synthesized": synthesized}


def _synthesize(
    succeeded, failed, llm_builder, recorded_dismissals=(), synthesizer: str = ""
) -> tuple[str, bool]:
    """Ask a model to cross-reference the structured findings into one report.

    Returns the report and whether the synthesizer actually wrote it.
    """
    # Only the synthesizer sees recorded dismissals. The reviewers stay blind
    # to them so their findings stay independent -- a dismissal changes how
    # something is reported, never whether it is found. Resolved upstream in
    # fetch_context_node, from the reviewed repo at its base ref.
    recorded = dismissals.render(list(recorded_dismissals))
    payload = json.dumps(
        [
            {
                "model": r.model,
                "findings": [f.model_dump() for f in r.findings],
                "low_confidence_reason": r.low_confidence_reason,
            }
            for r in succeeded
        ],
        indent=2,
    )
    header = (
        _panel_line(succeeded, failed, synthesizer)
        + _single_reviewer_note(succeeded, failed)
        + _failure_note(failed)
        + _diversity_note(succeeded)
        + _low_confidence_note(succeeded)
        + _malformed_note(succeeded)
    )

    try:
        llm = llm_builder(synthesizer)
        response = llm.invoke(
            [
                SystemMessage(content=SYNTHESIS_PROMPT),
                *([SystemMessage(content=recorded)] if recorded else []),
                HumanMessage(content=payload),
            ]
        )
        return header + _text_of(response), True
    except Exception as exc:  # noqa: BLE001 - the reviews already cost money; don't lose them
        return (
            header
            + f"> Synthesis failed ({type(exc).__name__}: {exc}). "
            "Raw findings from each model follow.\n\n"
            + _raw_listing(succeeded, recorded_dismissals)
        ), False


def _panel_line(succeeded, failed, synthesizer: str) -> str:
    """Name who reviewed and who wrote the report, with their efforts.

    With any model allowed in any role, a report no longer implies its own
    panel. A reader weighing "2 of 3 agreed" needs to see which 3, and at what
    effort, without digging up the workflow run that produced it.
    """
    reviewers = ", ".join(
        [f"`{r.model}`" for r in succeeded]
        + [f"`{r.model}` (did not report)" for r in failed]
    )
    return f"_Reviewers: {reviewers} · Synthesizer: `{synthesizer}`_\n\n"


def _single_reviewer_note(succeeded, failed) -> str:
    """Say plainly that a one-model run corroborated nothing."""
    if len(succeeded) != 1 or failed:
        return ""
    return (
        f"> **Single reviewer: `{succeeded[0].model}`.** Nothing here was corroborated "
        "by a second model, so every finding is unverified and there is no consensus "
        "to report. Treat this as one code review, not a triangulated one.\n\n"
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
    if len(succeeded) < 2 or len(providers) > 1 or None in providers:
        return ""
    return (
        f"> **All {len(succeeded)} reviewers are `{providers.pop()}` models.** Models from one "
        "provider share training data and failure modes, so agreement between them is much "
        "weaker evidence than cross-provider consensus. Read the sections below as one "
        "opinion stated repeatedly, not as independent corroboration.\n\n"
    )


def _low_confidence_note(succeeded) -> str:
    """Warn about reviewers whose empty result may be reasoning-budget exhaustion.

    Without this, a suspect empty result reads identically to a genuine clean
    pass in the synthesized report -- silently weakening both "unique insight"
    attribution and cross-model consensus.
    """
    flagged = [r for r in succeeded if r.low_confidence_reason]
    if not flagged:
        return ""
    lines = "\n".join(f"- `{r.model}`: {r.low_confidence_reason}" for r in flagged)
    return (
        f"> **{len(flagged)} reviewer(s) returned 0 findings flagged low-confidence.** "
        "Do not read these as a clean pass or as corroboration.\n"
        f"{lines}\n\n"
    )


def _malformed_note(succeeded) -> str:
    """Name the findings a reviewer returned that were set aside as malformed.

    The review still counts -- what did validate is in the report -- but a
    reader weighing "only one model found this" needs to know another model
    may have said something here that could not be kept.
    """
    flagged = [r for r in succeeded if r.malformed_findings]
    if not flagged:
        return ""
    lines = "\n".join(
        f"- `{r.model}`: {len(r.malformed_findings)} of "
        f"{len(r.malformed_findings) + len(r.findings)} set aside -- "
        + "; ".join(r.malformed_findings)
        for r in flagged
    )
    return (
        f"> **{len(flagged)} reviewer(s) returned findings that did not match the schema.** "
        "Those findings are not in this report.\n"
        f"{lines}\n\n"
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


def _raw_listing(succeeded, recorded_dismissals=()) -> str:
    """Each model's findings as-is, plus what a human already rejected.

    The dismissals are listed rather than matched to findings: the model that
    would do the matching is the one that just failed. Listing them keeps the
    ledger's rule -- downgrade, never hide -- in the one report where the reader
    has the least other help.
    """
    lines: list[str] = []
    for result in succeeded:
        lines.append(f"### {result.model} ({len(result.findings)} findings)")
        for finding in result.findings:
            lines.append(
                f"- **{finding.severity}/{finding.category}** `{finding.file}:{finding.line}` "
                f"— {finding.title}\n  {finding.detail}"
            )
        lines.append("")
    if recorded_dismissals:
        lines.append("### Previously rejected")
        lines.append(
            "A human reviewed and rejected these claims on this codebase. Any "
            "finding above that repeats one should be read as Optional unless "
            "this diff gives new evidence.\n"
        )
        lines.extend(d.render() for d in recorded_dismissals)
        lines.append("")
    return "\n".join(lines)

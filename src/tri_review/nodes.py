"""Graph nodes: three reviewers that never raise, and a synthesizer that reads them."""

from __future__ import annotations

import json

from langchain_core.messages import HumanMessage, SystemMessage

from . import cache, config, dismissals
from .errors import InsufficientReviewsError
from .providers import build_llm, provider_of
from .report import normalize, render_block, render_report
from .schema import NormalizedFinding, ReviewOutput, ReviewResult, SynthesisOutput, _as_block
from .state import ReviewState
from .suggestions import applyability, parse_hunks

REVIEW_PROMPT = """You are an expert code reviewer examining a pull request.

Focus on bugs, security vulnerabilities, logic errors, and performance problems.
Ignore pure style and formatting nitpicks -- a linter handles those.

You are given the PR diff and, where available, the full current contents of the
files it touches. Review the change, not the whole file: only report issues the
diff introduces or leaves unaddressed in the code it touches.

Each line of a file's current contents is prefixed with its line number and
` | ` (for example ` 42 | return token`); padding varies with the file's length. The prefix is a line number, not code.
Use these numbers for `line`, `fix_start_line` and `fix_end_line`, and never
include the prefix in a `replacement`.

Keep each finding short. `detail` is the problem: what is wrong and its
consequence, in at most two sentences. `trigger` is one sentence naming the
input, state or sequence of events that makes the problem happen. `suggested_fix`
is one sentence saying what to change. Offer a `replacement` only when the fix is
a local edit to the lines you name; set `fix_start_line` and `fix_end_line` to the
smallest range that contains the change, and leave all three null otherwise.

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

Return one entry in `findings` for each distinct issue. Match on the underlying
problem, not on wording -- the same bug described differently in the same file
and region is one finding, listed once with every model that reported it. You
do not write the report: tri-review lays it out from these fields, numbers the
findings, and checks each one's reporters, tag and fix against the reviews.

Fill in each field as follows:

- `reported_by`: the `model` of every reviewer that reported this issue,
  exactly as the input spells it. Leave it empty only for an issue you are
  adding yourself rather than lifting from a reviewer's finding.
- `severity`: the highest severity any of those reviewers gave it.
- `category`, `file`, `line`: as the reporters gave them. `line` is a line
  number in the PR head's version of `file`, or null.
- `title`: one line naming the problem.
- `problem`: what is wrong and its consequence, in at most two sentences.
- `trigger`: one sentence naming the input, state or sequence of events that
  makes the problem happen; an empty string if no reporter stated one.
- `fix`: one sentence saying what to change, or null.
- `fix_start_line`, `fix_end_line`, `replacement`: copied unchanged from the
  one reporter's finding whose fix matches `fix`. Never write, edit or combine
  replacement code yourself; if no reporter gave one, leave all three null.
- `action`: `blocking`, `blocking_pending_check` or `optional`, by the rules
  below.
- `check`: for `blocking_pending_check` only, the specific check that would
  settle it -- the call to make, the command to run, the page to read -- so
  the reader can resolve it in one step.
- `falsified_by`: for `blocking` and `blocking_pending_check`, what would show
  the finding to be wrong.
- `previously_rejected`, `new_evidence`: see the dismissals below; null
  otherwise.

`blocking` requires all three: two or more models reported the issue, it is
critical or major severity, and it is settled by what the diff and the file
contents in front of you actually show. Agreement alone is not evidence.
Reviewers who cannot observe something can still agree about it, and where
they share a wrong assumption their agreement reproduces it rather than
correcting it -- so treat consensus as a reason to look closely, not as proof.

`blocking_pending_check` is for an issue that would be `blocking` except that
it rests on behaviour none of the reviewers could observe: how a provider, API,
library, runtime, or model actually responds; what a service accepts or
returns; what a particular version does. Name the check that would settle it
in `check`. Never present an unobserved claim as a required change.

Everything else -- minor-severity issues, issues only one model reported, and
any issue you are adding yourself -- is `optional`. An issue whose falsifier
you cannot name is a speculation, not a requirement; make it `optional`.

You may be given a list of claims a human has already reviewed and rejected on
this codebase. Never silently drop a finding because it matches one. Report it,
put the recorded reason in `previously_rejected`, and make it `optional` --
unless this diff carries new evidence the earlier rejection did not account
for, in which case say what that new evidence is in `new_evidence`. A reader
must always be able to see that a finding was raised and on what grounds it was
set aside.

If the reviews report no issues, return an empty `findings` list.
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
            # No request was made, so the stored run's prompt cache usage
            # says nothing about this one.
            hit.prompt_cache_read_tokens = hit.prompt_cache_write_tokens = None
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
        cache_read, cache_write = _prompt_cache_usage(raw_result["raw"])
        result = ReviewResult(
            model=model_name,
            findings=findings,
            malformed_findings=malformed,
            low_confidence_reason=_low_confidence_reason(raw_result["raw"], findings),
            prompt_cache_read_tokens=cache_read,
            prompt_cache_write_tokens=cache_write,
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


# Where cache writes are reported. langchain-anthropic zeroes the generic
# `cache_creation` key when the API reports the per-TTL breakdown and moves the
# count to the ephemeral_* keys, so reading `cache_creation` alone shows a
# write as 0. langchain-openai prefixes both keys with the service tier on
# priority and flex (`priority_cache_read`), which a project's default tier can
# select without tri-review asking for it -- hence the suffix match below.
# langchain-google-genai reports reads only; Gemini's implicit cache has no
# write to report.
_CACHE_WRITE_KEYS = ("cache_creation", "ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")


def _prompt_cache_usage(raw_message) -> tuple[int | None, int | None]:
    """(cache read, cache write) input tokens, or None for whichever wasn't reported.

    Read as defensively as `_low_confidence_reason`: an absent or unexpected
    shape is no signal, never a failure.
    """
    usage = getattr(raw_message, "usage_metadata", None)
    details = usage.get("input_token_details") if isinstance(usage, dict) else None
    if not isinstance(details, dict):
        return None, None
    counts = [(k, v) for k, v in details.items() if isinstance(k, str) and isinstance(v, int)]
    reads = [v for k, v in counts if k == "cache_read" or k.endswith("_cache_read")]
    writes = [v for k, v in counts if k in _CACHE_WRITE_KEYS or k.endswith("_cache_creation")]
    return (
        sum(reads) if reads else None,
        sum(writes) if writes else None,
    )


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

    Returns the report, whether the synthesizer produced it, and `synthesis`:
    the normalized findings the report was rendered from, [] on the fallback.
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

    report, synthesized, synthesis = _synthesize(
        succeeded,
        failed,
        llm_builder,
        state.get("dismissals") or [],
        synthesizer,
        diff=state.get("diff") or "",
        link_repo=state.get("link_repo") or "",
        # `head_ref` holds the PR head's commit SHA (see ReviewState), which
        # the report's permalinks pin to.
        head_sha=state.get("head_ref") or "",
    )
    return {"final_report": report, "synthesized": synthesized, "synthesis": synthesis}


def _synthesize(
    succeeded,
    failed,
    llm_builder,
    recorded_dismissals=(),
    synthesizer: str = "",
    diff: str = "",
    link_repo: str = "",
    head_sha: str = "",
) -> tuple[str, bool, list[NormalizedFinding]]:
    """Ask a model to cross-reference the structured findings, then render them.

    The model returns `ReportFinding`s (ADR 0001). tri-review normalizes them
    (R4, R5), keeps only fixes a reporter offered (R3), checks each fix against
    the diff's hunks (R7), and renders the report itself (R6), so the model's
    tags, fixes and layout are never taken on trust. Returns the report,
    whether it was synthesized, and the findings it was rendered from: [] for
    the raw fallback, which has none.
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
        structured = llm.with_structured_output(SynthesisOutput, include_raw=True)
        raw_result = structured.invoke(
            [
                SystemMessage(content=SYNTHESIS_PROMPT),
                *([SystemMessage(content=recorded)] if recorded else []),
                HumanMessage(content=payload),
            ]
        )
        parsing_error = raw_result.get("parsing_error")
        if parsing_error is not None:
            # As in review_with: include_raw=True returns a parse failure
            # instead of raising it, and unchecked it would pass for a
            # synthesis that found nothing.
            raise parsing_error
        output = raw_result["parsed"]
        if output is None:
            # No tool call at all. Rendering that as an empty synthesis would
            # hide every reviewer's findings behind "No consensus findings."
            raise ValueError("returned no structured output")
        if output.malformed and not output.findings:
            raise ValueError(_all_malformed_error(output.malformed))
        # Only a review with findings can have reported an issue. A clean or
        # low-confidence empty review listed in `reported_by` would otherwise
        # count toward Consensus, and keep a Blocking tag nobody agreed to.
        reporters = [r.model for r in succeeded if r.findings]
        if not output.findings and reporters:
            # An empty list means the reviews found nothing. When they found
            # something, it would hide all of it behind "No consensus
            # findings." and be stored as this commit's answer.
            reported = sum(len(r.findings) for r in succeeded)
            raise ValueError(f"returned no findings, though the reviews reported {reported}")
        hunks = parse_hunks(diff)
        offered = _offered_fixes(succeeded)
        findings = [
            _with_applyability(_reporters_fix_only(finding, offered), hunks)
            for finding in normalize(output.findings, reporters)
        ]
        report = (
            header
            + _synthesis_malformed_note(output.malformed)
            + render_report(findings, link_repo, head_sha)
        )
    except Exception as exc:  # noqa: BLE001 - the reviews already cost money; don't lose them
        return (
            header
            + f"> Synthesis failed ({type(exc).__name__}: {exc}). "
            "Raw findings from each model follow.\n\n"
            + _raw_listing(succeeded, recorded_dismissals, link_repo, head_sha)
        ), False, []
    return report, True, findings


def _offered_fixes(succeeded) -> dict[str, dict[tuple, set[int | None]]]:
    """Every fix each reviewer returned, by model.

    Keyed by (file, start, end, replacement), each fix maps to the lines of the
    findings it was offered for, so a fix can be tied back to its issue.
    """
    offered: dict[str, dict[tuple, set[int | None]]] = {}
    for result in succeeded:
        fixes = offered.setdefault(result.model, {})
        for f in result.findings:
            fix = (f.file, f.fix_start_line, f.fix_end_line, f.replacement)
            fixes.setdefault(fix, set()).add(f.line)
    return offered


def _reporters_fix_only(
    finding: NormalizedFinding, offered: dict[str, dict[tuple, set[int | None]]]
) -> NormalizedFinding:
    """`finding`, keeping its fix only if one of its reporters offered exactly that fix (R3).

    The synthesizer copies fixes; it never writes them. Passed through, a
    replacement no reviewer offered is checked against the diff like any
    other and can be posted as a one-click suggested change: the wrong change
    ADR 0001 exists to prevent. So the fix range and replacement must equal,
    in the same file, a fix that a model in the normalized `reported_by`
    returned, and for this issue: the finding's line must equal the line of
    the finding that fix was offered for, or lie inside the fix's range.
    Otherwise a reporter's fix for one issue could be posted under another's
    title. Failing either, all three are dropped, as R4.1 drops an invented
    reporter, and the finding reads as one with no fix code.
    """
    fix = (finding.file, finding.fix_start_line, finding.fix_end_line, finding.replacement)
    if fix[1:] == (None, None, None):
        return finding
    start = finding.fix_start_line
    end = finding.fix_end_line if finding.fix_end_line is not None else start
    in_range = (
        finding.line is not None and start is not None and start <= finding.line <= end
    )
    for model in finding.reported_by:
        source_lines = offered.get(model, {}).get(fix)
        if source_lines is not None and (in_range or finding.line in source_lines):
            return finding
    return finding.model_copy(
        update={"fix_start_line": None, "fix_end_line": None, "replacement": None}
    )


def _with_applyability(finding: NormalizedFinding, hunks) -> NormalizedFinding:
    """`finding` with R7's verdict on its fix: applyable, or why it is not."""
    reason = applyability(finding, hunks)
    return finding.model_copy(update={"applyable": reason is None, "inline_reason": reason})


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


def _synthesis_malformed_note(malformed: list[str]) -> str:
    """Name the synthesized findings that were set aside as malformed (R3).

    The rest of the synthesis still stands, as a reviewer's review does when
    one of its findings is set aside, but the reader must know the synthesizer
    said more than the report shows.
    """
    if not malformed:
        return ""
    lines = "\n".join(f"- {entry}" for entry in malformed)
    return (
        f"> **The synthesizer returned {len(malformed)} finding(s) that did not match the "
        "schema.** Those findings are not in this report.\n"
        f"{lines}\n\n"
    )


def _failure_note(failed) -> str:
    if not failed:
        return ""
    lines = "\n".join(f"- `{r.model}`: {r.error}" for r in failed)
    return f"> **{len(failed)} model(s) did not report.** This review is based on the rest.\n{lines}\n\n"


def _raw_listing(succeeded, recorded_dismissals=(), link_repo: str = "", head_sha: str = "") -> str:
    """Each model's findings in the block format (R11), plus what a human already rejected.

    The blocks carry no ID, reporter line or suggestion status: nothing was
    synthesized, so there is no consensus to state and no payload to post.

    The dismissals are listed rather than matched to findings: the model that
    would do the matching is the one that just failed. Listing them keeps the
    ledger's rule -- downgrade, never hide -- in the one report where the reader
    has the least other help.
    """
    lines: list[str] = []
    for result in succeeded:
        lines.append(f"### {result.model} ({len(result.findings)} findings)")
        for finding in result.findings:
            block = render_block(_as_block(finding), link_repo, head_sha, with_id=False)
            # One heading level below the model's, so each block reads as one
            # of that model's findings rather than as the next model.
            lines += ["", "#" + block]
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

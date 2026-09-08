"""The one gate that guesses: does this diff change behaviour at all?

Path globs cannot answer this. `deploy.sh` is a reviewable file whether the
change is a rewrite of the rollout logic or a typo fix in a comment above it,
and telling those apart needs something that reads code.

Doing it by stripping comment-prefixed lines is a trap: `#` inside a string
literal, heredocs, docstring boundaries, and multi-line `/* */` all break it,
and several things that *look* like comments are not inert at all -- `# noqa`,
`// eslint-disable`, `#!/usr/bin/env`, `# type: ignore`, build pragmas. So the
question goes to the cheapest available model instead, with an explicit
instruction to answer "yes, review it" whenever there is any doubt.

The asymmetry is deliberate and total. Reviewing a PR that did not need it
wastes money. *Not* reviewing one that did is a silent miss on real code --
the worst thing this tool can do -- so every uncertain case, every parse
failure, and every provider error resolves toward reviewing.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from . import config
from .providers import build_llm

TRIAGE_PROMPT = """You are triaging a pull request diff to decide whether it is worth
a full code review by three separate models.

Answer `changes_behavior: false` ONLY if you are certain that every single change in
the diff is one of:
  - a comment or docstring whose text is purely explanatory
  - documentation, changelog, or README prose
  - whitespace, indentation, or formatting with no semantic effect
  - a string that is only ever displayed to a human and never parsed or compared

Answer `changes_behavior: true` for everything else, including anything that:
  - alters control flow, data, types, or arithmetic
  - touches configuration, dependencies, permissions, credentials, or CI
  - changes generated output, build steps, or migrations
  - is a comment that a tool reads as an instruction rather than prose --
    `# noqa`, `# type: ignore`, `# pylint: disable`, `// eslint-disable`,
    `// @ts-ignore`, `#!/usr/bin/env`, compiler or build pragmas, annotation
    comments consumed by a framework, or migration directives
  - you cannot fully see, because the diff is truncated or lacks context

If you are unsure for any reason at all, answer true. A needless review costs a
little money. A skipped review of real code is a bug that ships. Those are not
comparable, so do not treat them as a balanced judgement call.

State your reason in one short sentence."""


class TriageVerdict(BaseModel):
    """Whether the diff is worth reviewing, and why."""

    changes_behavior: bool = Field(
        description="True if any change could affect behaviour. True when unsure."
    )
    reason: str = Field(description="One short sentence explaining the call.")


def assess(diff: str, model: str | None = None, llm_builder=build_llm) -> TriageVerdict | None:
    """Ask whether `diff` is worth reviewing. None means "could not tell".

    None and True lead to the same place -- a review happens. They are kept
    distinct only so the user can be told which one occurred.
    """
    model = model or config.triage_model()
    try:
        llm = llm_builder(model)
        verdict = llm.with_structured_output(TriageVerdict).invoke(
            [SystemMessage(content=TRIAGE_PROMPT), HumanMessage(content=diff)]
        )
    except Exception:  # noqa: BLE001 - a failed gate must never block the review
        return None
    return verdict if isinstance(verdict, TriageVerdict) else None

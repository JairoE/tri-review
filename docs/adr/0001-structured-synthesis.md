# ADR 0001: The synthesizer returns structured findings; tri-review renders the report

**Status:** Accepted (implementation pending) · **Date:** 2026-10-18 ·
**Feature:** [Actionable Findings](../features/actionable-findings/PRD.md)

## Context

Reviewers already return structured `Finding`s (`src/tri_review/schema.py`).
The synthesizer does not: `_synthesize` in `src/tri_review/nodes.py` asks the
model for a free Markdown report and posts its text as-is. Everything about
that report's shape lives in `SYNTHESIS_PROMPT`:

- the section layout
- the severity labels
- the Blocking / Blocking pending check / Optional rules
- the "Falsified by" clause
- the handling of dismissed claims

The only checks on it are tests that assert on the prompt's wording.

Actionable Findings needs three things from the synthesis that prose cannot
reliably give:

- per-finding IDs that are the same in the report and in the inline comments
- exact fix ranges and replacement code, checked against the diff before
  anything is posted to GitHub
- the same Problem / Trigger / Fix shape every time

## Decision

The synthesizer is called with `with_structured_output(SynthesisOutput)`, and
returns a list of `ReportFinding` objects. tri-review then:

1. normalizes them, computing consensus from the models that actually
   reported and enforcing the tag rules in code;
2. assigns `F1..Fn`;
3. renders the Markdown report deterministically;
4. builds the GitHub review payload from the same normalized list.

The normalized list is stored in `RunRecord` with the rendered report.

## Consequences

- **The rules become enforced, not requested.** "Blocking requires consensus,
  severity and a falsifier" becomes a function with tests. A model that tags
  a single-model claim Blocking is downgraded, and the report says why.
- **The format is stable.** A synthesizer change, or an `@effort` change,
  can't change the report's layout.
- **New dependency on structured output for the synthesizer.** Every
  synthesizer model must support the forced tool call that reviewers already
  need. This narrows the choice of synthesizer to the same set as reviewers;
  `claude-sonnet-5-5` is already known to reject it (see
  `.github/workflows/tri-review.yml`).
- **Less room for the model's own wording.** Cross-finding commentary (for
  example, "these three findings share one root cause") has no field and is
  lost. If that turns out to matter, add an explicit `notes` field rather than
  going back to free text.
- **Stored history changes shape.** `SCHEMA_VERSION` goes to 2. Old records
  become misses, and each PR is re-reviewed once.
- **Unlocks later work.** "Resolved / still open / new" comparison between
  runs (anticipated in `history.py`'s module docstring) can diff structured
  findings instead of re-parsing prose.

## Alternatives considered

- **Keep prose and parse it.** Parse the synthesizer's Markdown for IDs, line
  ranges and code. Rejected: the parser would be as fragile as the model's
  adherence to the format, and the cost of a failure is a wrong one-click
  change posted to a PR.
- **Prose report plus a second structured call for fixes only.** Rejected:
  it doubles synthesis cost, and the two outputs can disagree about which
  findings exist, so IDs would not line up.
- **Build suggestions straight from the reviewers' raw findings, skipping the
  synthesizer.** Rejected: there's no deduplication, so three models
  reporting one bug would post three competing suggestions on the same lines.

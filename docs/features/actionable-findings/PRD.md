# Actionable Findings PRD

**Status:** Specified, not implemented ·
**Plan:** [`implementation-plan.md`](implementation-plan.md) ·
**Decision:** [ADR 0001](../../adr/0001-structured-synthesis.md) ·
**Product constraints:** [`docs/product/PRD.md`](../../product/PRD.md)

## Summary

Every finding tri-review reports states its **problem**, what **triggers** it,
and the **fix**, in a few sentences each, and is anchored to an exact
`file:start-end` range at the PR's head commit. When a finding carries exact
replacement code for lines inside the PR's diff, the GitHub Action also posts
it as an inline review comment with a GitHub *suggested change*, which anyone
with write access can apply with **Commit suggestion**.

## Problem and User Value

Today a finding is free prose written by the synthesizer
(`SYNTHESIS_PROMPT` in `src/tri_review/nodes.py`). Two problems follow:

1. **Findings are long and uneven.** Nothing bounds a finding's length or
   requires it to say what makes the bug happen. The format exists only as
   prompt wording, and models drift from it.
2. **Line numbers are approximate and nothing is applyable.** Reviewers see
   file bodies without line numbers (`ReviewContext.render` in
   `src/tri_review/context.py`); the README's own sample output reads
   "approx. lines 4–5". There is no structured fix range or replacement code,
   so a fix has to be retyped by hand.

**Value:** a reader can tell in three lines whether a finding is real and
when it bites, and can apply the fix in one click instead of transcribing it.

## Goals

- G1. Every finding renders in the same Problem / Trigger / Fix shape,
  produced by tri-review's own renderer, not by prompt wording.
- G2. Line numbers in findings are exact line numbers in the PR head's files.
- G3. A finding whose fix is exact code on diff lines can be applied on GitHub
  in one click.
- G4. The tagging rules currently stated only in `SYNTHESIS_PROMPT` (what may
  be Blocking) are enforced in code.

## Non-goals

- A `/tri-review apply` comment command, or tri-review committing to a PR
  branch itself. Applying is GitHub's native suggested-change flow.
- Finding IDs that stay stable across runs. `F1..Fn` are unique within one
  report only.
- Resolving or hiding earlier runs' suggestion threads. GitHub marks threads
  outdated when their lines change; nothing else is done.
- Suggestions on lines outside the diff. GitHub's API cannot anchor review
  comments there.
- Changing what reviewers look for, or the triangulation rules themselves.

## Users and Primary Flows

**PR author or reviewer on GitHub (Action):**
1. A push triggers the workflow; tri-review reviews the PR.
2. The report comment appears as today, with findings in the new block format.
3. A review titled "tri-review suggested changes" appears with one inline
   comment per applyable finding, on the exact lines.
4. The user reads the comment, clicks **Commit suggestion** (or adds several
   to a batch and commits once). GitHub commits under the user's identity.

**CLI user:**
1. `tri-review --pr 123` prints the report in the new format.
2. `--review-output review.json` additionally writes the GitHub review payload,
   for posting by the Action or by hand (`gh api ... --input review.json`).

**Claude Code skill user:** asks Claude to apply "F2"; Claude uses the
finding's `file:start-end` and code block from the report.

## Requirements

### R1. Line-numbered file context

- `ReviewContext.render()` prefixes every line of each "Current contents of
  `<path>`" block with its 1-indexed line number, right-aligned to the width
  of the file's line count, then ` | `: e.g. ` 42 | return token`.
- The diff block is unchanged.
- `REVIEW_PROMPT` states that the prefixes are line numbers, not code, that
  `line`, `fix_start_line` and `fix_end_line` must use them, and that a
  `replacement` must never contain them.
- `estimated_tokens` and the budget's largest-first dropping keep using
  `render()`, so the added prefix cost is accounted for.

### R2. Reviewer finding schema

`Finding` (`src/tri_review/schema.py`) gains:

| Field | Type | Meaning |
|---|---|---|
| `trigger` | `str` | One sentence: the input, state or sequence that makes the problem happen. |
| `fix_start_line` | `int \| None` | First line the fix replaces, 1-indexed, in the PR head's version of `file`. |
| `fix_end_line` | `int \| None` | Last line replaced, inclusive. Defaults to `fix_start_line`. |
| `replacement` | `str \| None` | Exact code that replaces those lines, with indentation, no line-number prefixes. `""` deletes them. `null` when the fix is not a local code change. |

Existing fields keep their names (history records and the dismissal ledger
refer to them); their schema descriptions tighten:

- `detail` is the **Problem**: what is wrong and its consequence, at most two
  sentences.
- `suggested_fix` is the **Fix**: one sentence saying what to change.

Validation (in a `mode="before"` validator, like `_title_from_detail`):

- A missing or blank `trigger` becomes `""`; the finding is kept. `trigger`
  stays required in the JSON schema the model is shown.
- `fix_end_line` missing → set to `fix_start_line`.
- `fix_start_line < 1`, `fix_end_line < fix_start_line`, or a `replacement`
  with no `fix_start_line` → all three fix fields become `null`; the finding
  is kept.

### R3. Structured synthesis

The synthesizer is called with `with_structured_output(SynthesisOutput,
include_raw=True)`, the same mechanism reviewers use. `SynthesisOutput.findings`
is a list of `ReportFinding`:

| Field | Type | Notes |
|---|---|---|
| `reported_by` | `list[str]` | Model specs, as given in the input, that reported this issue. |
| `severity` | `Severity` | Highest severity among reporters. |
| `category` | `Category` | |
| `file` | `str` | |
| `line` | `int \| None` | |
| `title` | `str` | One line. |
| `problem` | `str` | At most two sentences. |
| `trigger` | `str` | One sentence; `""` if no reporter stated one. |
| `fix` | `str \| None` | One sentence. |
| `fix_start_line`, `fix_end_line`, `replacement` | as in R2 | Taken from a reporter's fix; never invented by the synthesizer. |
| `action` | `"blocking" \| "blocking_pending_check" \| "optional"` | |
| `check` | `str \| None` | Required for `blocking_pending_check`: the call, command or page that settles it. |
| `falsified_by` | `str \| None` | Required for both Blocking tags. |
| `previously_rejected` | `str \| None` | Recorded dismissal reason, when the finding matches one. |
| `new_evidence` | `str \| None` | What in this diff the dismissal did not account for. |

Malformed entries are set aside one at a time, as `ReviewOutput` does, and
disclosed in the header. If the structured call fails or nothing validates,
synthesis has failed and the raw fallback (R11) applies. So has an empty
`findings` list when the reviews reported findings: it would hide them all.

### R4. Deterministic tag enforcement

tri-review normalizes every `ReportFinding` before rendering. The model's
claims are inputs, not facts:

1. `reported_by` is filtered to models that returned a successful review in
   this run with at least one finding (duplicates removed, input order kept).
   A review with no findings reported nothing, so it corroborates nothing.
2. **Consensus** = 2 or more remaining reporters. **Unique** = exactly 1.
   **Synthesizer suggestion** = 0.
3. `blocking` is downgraded to `optional` unless the finding is Consensus,
   severity is `critical` or `major`, and `falsified_by` is non-empty.
4. `blocking_pending_check` is downgraded to `optional` unless it meets rule
   3's consensus and severity conditions and both `check` and `falsified_by`
   are non-empty.
5. A finding with `previously_rejected` and no `new_evidence` is `optional`.
6. Synthesizer suggestions are always `optional`.

Every downgrade is visible: the rendered finding says why, e.g.
"_Optional — only one model reported this_".

### R5. IDs and order

After normalization, findings are sorted by action (`blocking`,
`blocking_pending_check`, `optional`), then severity (`critical`, `major`,
`minor`), then `file`, then `line` (nulls last). They are numbered `F1..Fn` in
that order. The same ID is used in the report, in Actionable Next Steps, and
in the inline comment.

### R6. Report layout

The header notes are unchanged: panel line, single-reviewer note, failure
note, diversity note, low-confidence note, malformed note. Then, rendered by
tri-review:

~~~markdown
## Consensus Findings

### F2 · **major** · bug — Token refresh races logout
[`src/auth.py:42-44`](https://github.com/o/r/blob/<sha>/src/auth.py#L42-L44) · `gpt-6-sol`, `claude-sonnet-5`

**Problem:** `refresh()` writes the new token after `logout()` cleared it.
**Trigger:** logout while a refresh request is in flight.
**Fix:** check `self._session` is still set before assigning (L42).

```python
if self._session is not None:
    self._session.token = new_token
```

_Inline suggestion available on L42-44._
~~~

- The location is `file:line`, or `file:start-end` when a fix range exists
  (the fix range wins over `line`), or just `file` when neither exists.
- It links to `https://github.com/{repo}/blob/{head_sha}/{file}#L{s}` or
  `#L{s}-L{e}` when both the repo slug and head SHA are known; otherwise it is
  plain code text.
- **Trigger** with an empty value renders as "_not stated_". **Fix** with no
  value is omitted. The code block appears only when `replacement` is set;
  its language comes from the file extension (none if unknown).
- **Unique Insights** blocks start the reporter line with
  "**`<model>`** (unverified by the other reviewers)". Synthesizer suggestions
  are listed under Unique Insights with "suggested by the synthesizer, not
  reported by any reviewer".
- A previously rejected finding shows "Previously rejected: <reason>" and,
  if present, "New evidence: <text>".
- The last line of each block is the suggestion status (R7): "Inline
  suggestion available on L42-44.", "No inline suggestion: <reason>.", or
  nothing when the finding has no replacement. The report marks eligibility
  only, because posting happens later and may not happen at all.
- **Actionable Next Steps** is one bullet per finding with a fix, in ID order:
  `**Blocking** F2 · `src/auth.py:42` — <fix>. Falsified by: <text>`.
  `blocking_pending_check` adds `Check: <check>.`
- An empty section is one line, e.g. "_No consensus findings._"

### R7. Applyable fixes

A finding gets an inline suggestion only if all hold:

1. `replacement` is not `null` and `fix_start_line` is set.
2. `file` is in the diff and not deleted (`+++ /dev/null`).
3. `fix_start_line..fix_end_line` lies within the new-side line range of one
   single hunk of that file's diff (context and added lines both count).
4. The range is at most **40** lines (Q2).
5. The finding is not `previously_rejected` without `new_evidence` (Q1).
6. The finding has at least one reporter (Q3).

A replacement that contains code fences is still applyable (Q4). Every fence
tri-review wraps around a `replacement`, both the report's code block (R6) and
the ```` ```suggestion ```` block (R8), is one backtick longer than the longest
run of backticks that starts any line of the replacement, and never shorter
than three.

Each failed condition has a fixed reason string used in R6, e.g.
"lines outside the diff", "range spans more than one hunk",
"range longer than 40 lines".

### R8. Review payload (`--review-output`)

New CLI option `--review-output PATH`. When at least one finding is applyable,
tri-review writes the exact body for
`POST /repos/{owner}/{repo}/pulls/{n}/reviews`:

```json
{
  "commit_id": "<head_sha>",
  "event": "COMMENT",
  "body": "<!-- tri-review-suggestions -->\n**tri-review suggested changes** for `<short sha>`: 3 fixes from the report. Unverified ones were reported by one model only.",
  "comments": [
    {
      "path": "src/auth.py",
      "start_line": 42, "start_side": "RIGHT",
      "line": 44, "side": "RIGHT",
      "body": "<!-- tri-review-suggestion:9f3c0a1b2d4e5f60 -->\n### F2 · **major** · bug — ...\n...\n```suggestion\n...\n```"
    }
  ]
}
```

- Single-line fixes omit `start_line` and `start_side`.
- The comment body is the R6 finding block, minus the location line and the
  fenced code block, plus a ```` ```suggestion ```` block holding
  `replacement` verbatim (an empty block for a deletion), fenced per R7's
  fence rule. A Unique finding's
  body starts with "**Unverified:** only `<model>` reported this."
- The fingerprint is the first 16 hex characters of SHA-256 over
  `path`, `fix_start_line`, `fix_end_line`, `replacement`, `title`, joined
  with `\0`.
- The file is **not written** (and any existing file at PATH is left alone)
  when no finding is applyable, when synthesis failed, or when the head SHA is
  unknown. The CLI prints one line either way:
  `3 inline suggestions (2 not applyable: lines outside the diff ×2)` or
  `No inline suggestions.`

### R9. Action posts the suggestions

- New input `suggestions`, default `'true'`: "Post applyable fixes as inline
  suggested changes."
- The "Run tri-review on this PR" step passes
  `--review-output "$RUNNER_TEMP/tri-review/review.json"` (cleared first, like
  `report.md`) and sets output `review-path` when the file exists.
- New step "Post inline suggestions", after "Post PR comment", runs when
  `post-comment` and `suggestions` are `'true'`, a PR number is known, and
  `review-path` is set. It runs `scripts/pr_review.sh`, which:
  1. resolves the bot login with `resolve_bot_login` (sourced from
     `pr_comment.sh`);
  2. lists that login's review comments on the PR
     (`GET /pulls/{n}/comments --paginate`) and collects their
     `tri-review-suggestion` fingerprints;
  3. drops payload comments whose fingerprint is already posted; if none
     remain, prints "All suggestions already posted." and exits 0;
  4. posts the review (`POST /pulls/{n}/reviews --input`);
  5. on HTTP 422, posts each remaining comment alone via
     `POST /pulls/{n}/comments` with `commit_id`, emitting a `::warning::`
     per comment that fails;
  6. writes `suggestions-posted=<count>` to `$GITHUB_OUTPUT`.
- Every failure in this step (403 on fork PRs, 422, network) is a
  `::warning::`; the script always exits 0. The step never affects
  "Enforce result".
- New Action output `suggestions-posted` (empty when the step did not run).
- Capped, skipped and failed runs produce no `review-path`, so nothing posts.
- No new workflow permissions: review comments need `pull-requests: write`,
  which the workflow already grants.

### R10. History and replay

- `RunRecord` (`src/tri_review/history.py`) stores the normalized synthesis
  (`synthesis: list[NormalizedFinding]`, which records each finding's ID,
  kind and applyability) alongside `report`.
- `SCHEMA_VERSION` becomes `2`. Version-1 records are misses, so each PR is
  re-reviewed once instead of replaying the old format.
- `_replay` writes `--review-output` from the stored synthesis; the head
  SHA is unchanged, so its stored applyability still holds. Fingerprint dedup (R9) makes re-posting a no-op.

### R11. Fallback and per-model rendering

- When synthesis fails, `_raw_listing` renders each model's findings in the
  R6 block format under a per-model heading, without IDs, Next Steps or a
  review payload.
- `render_findings_md` (one model's standalone review) uses the same block
  format.

## UX and Content

- Labels are exactly **Problem:**, **Trigger:**, **Fix:**; one per line.
- `REVIEW_PROMPT` asks for: problem ≤ 2 sentences, trigger 1 sentence, fix 1
  sentence, and a `replacement` only when the fix is a local edit to the lines
  named. It asks for the smallest range that contains the change.
- The synthesis prompt keeps the existing guidance on matching findings,
  evidence vs. agreement, and dismissals, and now explains each
  `ReportFinding` field instead of Markdown layout.
- Severity stays visible in every heading. The "unverified" label stays on
  every Unique finding, in the report and in its inline comment.

## Data and API Contract

- **Reviewer output:** `ReviewOutput{findings: list[Finding]}` with R2's
  fields. Changing it changes `_output_fingerprint()`, so the per-model cache
  invalidates automatically.
- **Synthesis output:** `SynthesisOutput{findings: list[ReportFinding]}` (R3).
- **Graph state** (`src/tri_review/state.py`): new `synthesis:
  list[NormalizedFinding]` (a `ReportFinding` plus `id`, `kind`,
  `downgrade_reason`, `inline_reason`, `applyable`) written by `synthesize_node` (empty on fallback), and
  new `link_repo: str` ("owner/name" for permalinks, `""` when unknown) set by
  the CLI.
- **CLI:** new `--review-output PATH`. All existing flags unchanged.
- **Action:** new input `suggestions`, new outputs `review-path` and
  `suggestions-posted`.
- **GitHub APIs:** `POST /repos/{o}/{r}/pulls/{n}/reviews`,
  `POST /repos/{o}/{r}/pulls/{n}/comments`,
  `GET /repos/{o}/{r}/pulls/{n}/comments`.

## Rules and Edge Cases

| Case | Behaviour |
|---|---|
| Finding with `line=None` and no fix range | Location is `file` only; no suggestion. |
| Fix range in a deleted file | Not applyable: "file deleted in this PR". |
| Fix range spanning two hunks | Not applyable: "range spans more than one hunk". |
| Replacement containing a ```` ``` ```` line | Applyable; the suggestion and report fences grow to four or more backticks (R7). |
| `replacement == ""` | Applyable deletion; suggestion block is empty. |
| Synthesizer names a model not on the panel | Dropped from `reported_by` (R4.1). |
| Panel of one | Every finding is Unique; suggestions allowed, all labelled unverified. |
| File contents dropped by the token budget | Hunks still parse, so anchors still validate; line numbers come from the hunk headers the model saw. |
| cwd mode with no resolvable repo slug | Plain-text locations; payload still written (it needs no slug). |
| Same suggestion on a re-run | Not reposted (fingerprint dedup). |
| Fork PR with a read-only token | Warning, nothing posted, job result unchanged. |
| Synthesis failed | Raw fallback (R11); no payload; not stored, as today. |

## Acceptance Criteria

- **AC1** `test_context.py`: rendered file contents carry right-aligned line
  numbers; the diff block does not.
- **AC2** `test_schema.py`: a finding with no `trigger` is kept with `""`; an
  inverted range clears all three fix fields and keeps the finding;
  `fix_end_line` defaults to `fix_start_line`.
- **AC3** `test_report.py`: each R4 downgrade rule, invented reporters, R5
  ordering and IDs, and the R6 block, including link vs. plain location.
- **AC4** `test_suggestions.py`: each R7 condition passes and fails with its
  reason string; payload shape and fingerprint stability match R8; a
  replacement containing ```` ``` ```` gets a longer suggestion fence.
- **AC5** `test_pr_review.py` (stub `gh`): posts once; a second run with the
  same payload posts nothing; a 422 falls back to per-comment posts; a 403
  warns and exits 0.
- **AC6** Live, on this repository: a throwaway PR with a planted bug gets a
  report whose finding cites the exact line, and an inline suggestion that
  **Commit suggestion** applies cleanly with the intended change. The same PR
  carries one suggestion whose replacement contains ```` ``` ````, which also
  applies cleanly.
- **AC7** The full `uv run pytest -q` suite is green after every task.

## Observability and Privacy

- The CLI prints the R8 summary line after the report.
- The Action exposes `suggestions-posted`, and the step log names every
  comment that failed to post and why.
- No new data leaves the runner. Suggestions contain only model output about
  the PR's own code, posted to the same PR.

## Rollout or Migration

- The per-model cache and history invalidate on their own (schema change and
  `SCHEMA_VERSION` 2). Cost: one paid re-review per PR on its next run.
- This repository's CI runs the pinned SHA in
  `.github/workflows/tri-review.yml`, so it picks up the feature only after
  that pin is bumped to the merged commit, in a separate PR.
- README documents the new input, the new format, and that fork PRs get no
  inline suggestions with the default token.

## Open Questions

None open. Resolved on 2026-10-19:

- **Q1.** Do previously rejected findings get inline suggestions?
  **No** (R7.5), unless the synthesizer cites new evidence.
- **Q2.** Maximum lines per suggestion: **40** (R7.4).
- **Q3.** Do synthesizer suggestions with no reporter go inline?
  **No** (R7.6).
- **Q4.** Does GitHub honour a fence longer than three backticks for a
  ```` ```suggestion ```` block? **Yes.** Replacements containing
  ```` ``` ```` are applyable, with a lengthened fence (R7 fence rule). AC6
  confirms it live.

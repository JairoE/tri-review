# Actionable Findings Implementation Plan

**Goal:** Findings render as Problem / Trigger / Fix blocks anchored to exact
lines, and applyable fixes are posted as inline GitHub suggested changes.

**Architecture:** Reviewers see line-numbered files and return fix ranges plus
replacement code. The synthesizer returns structured `ReportFinding`s
([ADR 0001](../../adr/0001-structured-synthesis.md)). tri-review normalizes
them, checks each fix against the diff's hunks, and renders both outputs from
the same normalized list: the Markdown report and a GitHub review payload,
which the Action posts with a new, tested shell script.

**Tech Stack:** Python 3.11, Pydantic v2, LangChain `with_structured_output`,
LangGraph, Click, Rich, Bash + `jq` + `gh api`, pytest with a stub `gh`.

**Spec:** `docs/features/actionable-findings/PRD.md`

## Global Constraints

From [`docs/product/PRD.md`](../../product/PRD.md), verbatim:

1. **Reviewing never executes PR code.** PR-authored files are read as data.
   The workflow pins the Action to a SHA on `main`, never `uses: ./`.
2. **Reviewers stay independent.** No reviewer sees another's output or the
   dismissal ledger. Only the synthesizer cross-references.
3. **Structured findings, not prose.** Reviewers return the `Finding` schema
   (`src/tri_review/schema.py`); that structure is what makes cross-model
   matching reliable.
4. **One flaky provider never aborts a run.** Review nodes record failures
   instead of raising; synthesis needs two successful reviews (one for a
   panel of one, reported as uncorroborated).
5. **Dismissals downgrade, never hide.** A finding matching a recorded
   dismissal is still reported, marked previously rejected, and made Optional.
6. **Agreement is not evidence.** Blocking requires consensus *and* severity
   *and* something the diff actually shows; unobservable claims are
   "Blocking pending check" with the check named.
8. **The Action's pass/fail is the review's result.** It is decided in the
   "Enforce result" step from the CLI's exit code, after the report is posted.

From the feature PRD, verbatim:

- Every failure in this step (403 on fork PRs, 422, network) is a
  `::warning::`; the script always exits 0. The step never affects
  "Enforce result". (R9)
- Existing fields keep their names (history records and the dismissal ledger
  refer to them). (R2)
- The file is **not written** (and any existing file at PATH is left alone)
  when no finding is applyable, when synthesis failed, or when the head SHA is
  unknown. (R8)

## Model and effort routing

Each task lists two **Implement** options and two **Review** options, as
`model @ effort`. The first option is the default. The second is the
alternative when the first is unavailable (spend limit) or when the task
turns out sharper or blunter than expected.

**No session, subagent or workflow stage runs on Fable.** Where the work calls
for Fable, Opus runs at a higher effort instead: Fable @ high becomes
Opus @ xhigh, and Fable @ ultracode becomes Opus @ max. Pass the model and
effort explicitly when starting a session; never let one inherit a default.

The rest of the calibration:

- novel or hard-constraint work goes to Opus @ xhigh or max;
- well-specified mechanical work goes to a Sonnet subagent;
- review of a finished diff goes to Opus;
- docs stay at low or medium effort.

"Review" means one single-pass review of that task's commit (the
`code-review` skill at the given effort). The single pre-merge review for the
whole branch is separate (see the end of this plan).

| # | Task | Implement (default / alt) | Review (default / alt) |
|---|---|---|---|
| 0 | Spec docs | Opus @ medium / Sonnet @ medium | Opus @ medium / Sonnet @ high |
| 1 | Line-numbered context | Sonnet @ medium / Opus @ medium | Opus @ medium / Sonnet @ high |
| 2 | Finding schema fields | Sonnet @ high / Opus @ medium | Opus @ high / Sonnet @ high |
| 3 | ReportFinding + normalization | Opus @ high / Opus @ xhigh | Opus @ high / Opus @ xhigh |
| 4 | Diff hunks + applyability | Opus @ high / Opus @ xhigh | Opus @ high / Opus @ xhigh |
| 5 | Deterministic renderer | Sonnet @ high / Opus @ medium | Opus @ medium / Sonnet @ high |
| 6 | Structured synthesis call | Opus @ max / Opus @ xhigh | Opus @ high / Opus @ xhigh |
| 7 | Review payload builder | Opus @ medium / Sonnet @ high | Opus @ high / Sonnet @ high |
| 8 | CLI flag + history v2 + replay | Opus @ medium / Sonnet @ high | Opus @ high / Opus @ medium |
| 9 | Action posting script | Opus @ high / Opus @ xhigh | Opus @ xhigh / Opus @ max |
| 10 | README + skill docs | Sonnet @ medium / Opus @ low | Sonnet @ medium / Opus @ low |

## File Structure

| Path | Change | Responsibility |
|---|---|---|
| `src/tri_review/context.py` | modify | `ReviewContext.render()` numbers file lines (R1) |
| `src/tri_review/nodes.py` | modify | `REVIEW_PROMPT` (R1, R2), new `SYNTHESIS_PROMPT`, structured `_synthesize`, `_raw_listing` in block format (R3, R11) |
| `src/tri_review/schema.py` | modify | `Finding` fields + validator (R2); `ReportFinding`, `SynthesisOutput`, `NormalizedFinding` (R3); `render_findings_md` in block format (R11) |
| `src/tri_review/report.py` | create | `normalize()` (R4, R5), `render_block()`, `render_report()` (R6) |
| `src/tri_review/suggestions.py` | create | `parse_hunks()`, `applyability()` (R7), `fingerprint()`, `build_review_payload()` (R8) |
| `src/tri_review/state.py` | modify | `synthesis`, `link_repo` keys |
| `src/tri_review/graph.py` | modify | none expected beyond passing state; verify in Task 6 |
| `src/tri_review/cli.py` | modify | `--review-output`, `link_repo` in initial state, summary line, replay writes payload (R8, R10) |
| `src/tri_review/history.py` | modify | `RunRecord.synthesis`, `SCHEMA_VERSION = 2` (R10) |
| `scripts/pr_review.sh` | create | dedup + post review + per-comment fallback (R9) |
| `action.yml` | modify | `suggestions` input, `--review-output`, `review-path` / `suggestions-posted` outputs, "Post inline suggestions" step (R9) |
| `tests/test_context.py` | modify | AC1 |
| `tests/test_schema.py` | create | AC2 |
| `tests/test_report.py` | create | AC3 |
| `tests/test_suggestions.py` | create | AC4 |
| `tests/test_render.py` | modify | R11 block format |
| `tests/test_synthesizer.py` | modify | structured synthesis; prompt-wording tests replaced by enforcement tests |
| `tests/test_cli.py`, `tests/test_history.py` | modify | R8 CLI behaviour, R10 |
| `tests/test_pr_review.py` | create | AC5, stub `gh` in the style of `tests/test_pr_comment.py` |
| `tests/test_action_inputs.py` | modify | new input is optional and wired |
| `README.md`, `.claude/skills/tri-review/SKILL.md` | modify | docs |

Commands. Red means the new tests fail, for the stated reason, and nothing
else fails. Green means the whole suite passes:

- red: `uv run pytest tests/<file> -q`
- green: `uv run pytest -q`

---

## Task 0: Spec documents

**Status:** done.

**Implement:** Opus @ medium / Sonnet @ medium · **Review:** Opus @ medium / Sonnet @ high

- **Files:**
  - `git mv prd.md docs/archive/mvp/PRD.md`
  - `git mv implementation-plan.md docs/archive/mvp/implementation-plan.md`
  - create `docs/product/PRD.md`, this PRD and plan, and `docs/adr/0001-structured-synthesis.md`
- **Check:** `grep -rn "prd.md" --include=*.md .` finds no stale links; `uv run pytest -q` is unchanged.
- **Commit:** `docs: actionable-findings spec and ADR; archive MVP docs`

## Task 1: Line-numbered file context (R1)

**Status:** done.

**Implement:** Sonnet @ medium / Opus @ medium · **Review:** Opus @ medium / Sonnet @ high

- **Files:** modify `src/tri_review/context.py`, `src/tri_review/nodes.py` (`REVIEW_PROMPT`); test `tests/test_context.py`.
- **Consumes:** `ReviewContext.files: dict[str, str]`.
- **Produces:** `_numbered(content: str) -> str` in `context.py`; `render()` uses it for every "Current contents" block.
- **Red:**
  - `test_file_contents_are_line_numbered`: a 12-line file renders `" 1 | ..."` through `"12 | ..."`, padded to width 2.
  - `test_diff_block_is_not_numbered`: the diff section is byte-identical to before.
  - `test_estimated_tokens_counts_the_numbering`: a numbered render estimates more tokens than the raw content.
  - `test_review_prompt_explains_line_number_prefixes`: `REVIEW_PROMPT` mentions the prefix and forbids it in `replacement`.

  Expected red: the assertions fail on the unnumbered output.
- **Implement:** `_numbered` uses `str.splitlines()` and right-aligns to `len(str(line_count))`. Add one paragraph to `REVIEW_PROMPT`.
- **Green:** `uv run pytest -q`. Existing `test_over_budget_drops_largest_first` must still pass; if its budget numbers shift, adjust the fixture sizes, not the rule.
- **Commit:** `feat(context): number file lines so findings cite exact lines`

## Task 2: Finding schema fields (R2)

**Status:** done.

**Implement:** Sonnet @ high / Opus @ medium · **Review:** Opus @ high / Sonnet @ high

- **Files:** modify `src/tri_review/schema.py`, `src/tri_review/nodes.py` (`REVIEW_PROMPT`); create `tests/test_schema.py`.
- **Consumes:** existing `Finding`, `_title_from_detail` pattern.
- **Produces:** `Finding.trigger: str`, `fix_start_line`, `fix_end_line: int | None`, `replacement: str | None`; validator `_normalize_fix`.
- **Red** (`tests/test_schema.py`):
  - `test_missing_trigger_is_kept_as_empty`
  - `test_trigger_is_required_in_the_model_facing_schema`: `"trigger"` is in `Finding.model_json_schema()["required"]`.
  - `test_end_line_defaults_to_start_line`
  - `test_inverted_range_clears_fix_and_keeps_finding`
  - `test_replacement_without_start_line_clears_fix`
  - `test_zero_start_line_clears_fix`
  - `test_empty_replacement_is_a_deletion_not_null`
  - `test_old_history_findings_still_validate`: a dict without the new fields validates.

  Expected red: a `ValidationError` or attribute errors on the new fields.
- **Implement:** add the fields with descriptions per R2; extend the `mode="before"` validator; add the R2 / UX length guidance to `REVIEW_PROMPT`.
- **Green:** `uv run pytest -q`. The cache key changes through `_output_fingerprint`; no cache test should need editing.
- **Commit:** `feat(schema): trigger, fix range and replacement on findings`

## Task 3: ReportFinding and normalization (R3 schema, R4, R5)

**Status:** done.

**Implement:** Opus @ high / Opus @ xhigh · **Review:** Opus @ high / Opus @ xhigh

- **Files:** modify `src/tri_review/schema.py`; create `src/tri_review/report.py`, `tests/test_report.py`.
- **Consumes:** `Severity`, `Category`, `SEVERITY_ORDER`, `ReviewOutput._set_aside_malformed` pattern.
- **Produces:**
  - `ReportFinding` with the R3 fields.
  - `SynthesisOutput{findings, malformed}`, with per-entry tolerance like `ReviewOutput`.
  - `NormalizedFinding(ReportFinding)` with `id: str`, `kind: Literal["consensus", "unique", "synthesizer"]`, `downgrade_reason: str | None`, `inline_reason: str | None`, `applyable: bool = False`.
  - `report.normalize(findings: list[ReportFinding], reported: list[str]) -> list[NormalizedFinding]`.
- **Red** (`tests/test_report.py`), one test per R4 rule:
  - `test_unknown_reporters_are_dropped`
  - `test_two_reporters_is_consensus_one_is_unique_zero_is_synthesizer`
  - `test_blocking_single_model_is_downgraded_with_reason`
  - `test_blocking_minor_is_downgraded`
  - `test_blocking_without_falsifier_is_downgraded`
  - `test_pending_check_without_check_is_downgraded`
  - `test_previously_rejected_without_new_evidence_is_optional`
  - `test_previously_rejected_with_new_evidence_keeps_its_tag`
  - `test_synthesizer_suggestion_is_always_optional`
  - `test_ids_follow_action_then_severity_then_file_then_line`
  - `test_null_lines_sort_last`
  - `test_one_malformed_report_finding_costs_only_itself`

  Expected red: `ImportError` for `tri_review.report`.
- **Implement:** pure functions only; no LLM, no I/O.
- **Green:** `uv run pytest -q`
- **Commit:** `feat(report): normalize synthesized findings and enforce tag rules`

## Task 4: Diff hunks and applyability (R7)

**Status:** done.

**Implement:** Opus @ high / Opus @ xhigh · **Review:** Opus @ high / Opus @ xhigh

- **Files:** create `src/tri_review/suggestions.py`, `tests/test_suggestions.py`.
- **Consumes:** unified diff text, as in `ReviewState["diff"]`; the path rules of `context.parse_changed_files` (renames use the post-image path; `+++ /dev/null` means deleted).
- **Produces:**
  - `parse_hunks(diff: str) -> dict[str, FileHunks]`, where `FileHunks{deleted: bool, ranges: list[tuple[int, int]]}` holds the new-side inclusive ranges from `@@ -a,b +c,d @@`.
  - `applyability(finding: NormalizedFinding, hunks) -> str | None`: `None` when applyable, else one of the fixed reason strings:
    - `"no replacement code"`
    - `"file not in this diff"`
    - `"file deleted in this PR"`
    - `"lines outside the diff"`
    - `"range spans more than one hunk"`
    - `"range longer than 40 lines"`
    - `"previously rejected"`
    - `"no reviewer reported it"`
  - `MAX_SUGGESTION_LINES = 40`
- **Red:**
  - one passing and one failing case per R7 condition
  - `test_hunk_with_zero_new_lines_has_no_range`: `+c,0`
  - `test_hunk_header_without_count_is_one_line`: `+c`
  - `test_rename_is_keyed_by_new_path`
  - `test_no_newline_at_end_of_file_marker_is_ignored`
  - `test_truncated_diff_still_parses_complete_hunks`

  Expected red: `ImportError`.
- **Implement:** a line-based parser; the first failing condition wins, in R7's order.
- **Green:** `uv run pytest -q`
- **Commit:** `feat(suggestions): parse diff hunks and decide which fixes are applyable`

## Task 5: Deterministic renderer (R6, R11)

**Status:** done.

**Implement:** Sonnet @ high / Opus @ medium · **Review:** Opus @ medium / Sonnet @ high

- **Files:** modify `src/tri_review/report.py`, `src/tri_review/schema.py` (`render_findings_md`); tests `tests/test_report.py`, `tests/test_render.py`.
- **Consumes:** `NormalizedFinding` (Task 3) with `applyable` / `inline_reason` set (Task 4).
- **Produces:**
  - `render_block(f, link_repo: str, head_sha: str, *, with_id: bool = True) -> str`
  - `render_report(findings, link_repo, head_sha) -> str`: the three sections plus Next Steps, without the header notes.
  - `language_for(path) -> str`
  - `fence_for(text: str) -> str`: backticks one longer than the longest
    backtick run that starts a line of `text`, minimum three (R7 fence rule)
- **Red:**
  - `test_block_has_problem_trigger_fix_lines`
  - `test_empty_trigger_renders_not_stated`
  - `test_fix_range_wins_over_line_in_location`
  - `test_location_links_to_head_when_repo_and_sha_known`
  - `test_location_is_plain_without_repo`
  - `test_unique_block_names_model_as_unverified`
  - `test_synthesizer_suggestion_is_labelled`
  - `test_downgrade_reason_is_shown`
  - `test_previously_rejected_reason_and_new_evidence_shown`
  - `test_inline_status_line_posted_vs_reason`
  - `test_next_steps_one_bullet_per_fix_with_falsifier_or_check`
  - `test_empty_sections_say_so_in_one_line`
  - `test_code_fence_language_from_extension`
  - `test_fence_for_is_three_backticks_for_plain_code`
  - `test_fence_for_outgrows_backticks_in_replacement`
  - `test_report_code_block_uses_a_longer_fence_when_needed`
  - updated `test_render.py` cases expect the block format

  Expected red: an `AttributeError` for `render_block`, or old-format assertions failing.
- **Implement:** string building only. `render_findings_md` wraps each `Finding` into a block via a small adapter (`with_id=False`).
- **Green:** `uv run pytest -q`
- **Commit:** `feat(report): render Problem/Trigger/Fix blocks with permalinks`

## Task 6: Structured synthesis call (R3 call, R11 fallback)

**Status:** done.

**Implement:** Opus @ max / Opus @ xhigh · **Review:** Opus @ high / Opus @ xhigh

The default is Opus @ max because this task is novel design on the
hard-constraint surface. It must keep constraints 2, 4, 5 and 6, and preserve
every header note and the fallback's no-store behaviour.

- **Files:** modify `src/tri_review/nodes.py`, `src/tri_review/state.py`, `src/tri_review/graph.py` (if needed); test `tests/test_synthesizer.py`.
- **Consumes:** `normalize`, `parse_hunks`, `applyability`, `render_report`, `render_block`; `state["diff"]`, `state["head_ref"]`, and new `state["link_repo"]`.
- **Produces:**
  - `synthesize_node` returns `{"final_report", "synthesized", "synthesis": list[NormalizedFinding]}`; the fallback's `synthesis` is `[]`.
  - New `SYNTHESIS_PROMPT`: field-by-field guidance, keeping the matching, evidence and dismissal guidance.
  - `ReviewState.synthesis`, `ReviewState.link_repo`.
- **Red:**
  - Rewrite the `CapturingLLM` stub to support `with_structured_output(schema, include_raw=True)`, as `tests/test_nodes.py::FakeLLM` does.
  - `test_synthesis_is_requested_as_structured_output`
  - `test_report_is_rendered_by_tri_review_not_the_model`: the stub returns a structured object, and the report contains `F1`.
  - `test_model_tagged_single_model_blocking_is_rendered_optional`: replaces `test_blocking_requires_evidence_not_just_agreement`.
  - `test_pending_check_without_check_renders_optional`: replaces `test_blocking_pending_check_must_name_the_check`.
  - `test_every_rendered_finding_carries_its_severity`: replaces `test_every_finding_bullet_must_carry_its_severity`.
  - `test_parsing_error_falls_back_to_raw_listing_in_block_format`
  - `test_all_malformed_synthesis_falls_back`
  - `test_fallback_returns_empty_synthesis`
  - `test_applyability_is_set_from_the_diff_in_state`
  - Keep every header-note, dismissal, panel-line and insufficient-review test passing unchanged.

  Expected red: the stub has no `with_structured_output`, or the report lacks IDs.
- **Implement:** mirror `review_with`'s `include_raw` / `parsing_error` handling; keep the `except Exception` fallback; `_raw_listing` uses `render_block(..., with_id=False)`.
- **Green:** `uv run pytest -q`
- **Commit:** `feat(synth): structured synthesis output rendered by tri-review`

## Task 7: Review payload builder (R8)

**Status:** done.

**Implement:** Opus @ medium / Sonnet @ high · **Review:** Opus @ high / Sonnet @ high

- **Files:** modify `src/tri_review/suggestions.py`; test `tests/test_suggestions.py`.
- **Consumes:** `list[NormalizedFinding]` with `applyable`; `head_sha`.
- **Produces:**
  - `fingerprint(f) -> str`: 16 hex characters.
  - `comment_body(f) -> str`
  - `build_review_payload(findings, head_sha) -> dict | None`: `None` when nothing is applyable or `head_sha` is empty.
- **Red:**
  - `test_payload_shape_matches_reviews_api`: keys `commit_id`, `event == "COMMENT"`, `body` starting with `<!-- tri-review-suggestions -->`, and `comments`.
  - `test_single_line_fix_omits_start_line`
  - `test_multi_line_fix_sets_start_and_side_right`
  - `test_comment_body_starts_with_fingerprint_marker`
  - `test_suggestion_block_holds_replacement_verbatim`
  - `test_deletion_is_an_empty_suggestion_block`
  - `test_suggestion_fence_outgrows_backticks_in_replacement`: a replacement
    containing a ```` ``` ```` line is wrapped in ```` ````suggestion ````.
  - `test_unique_comment_is_marked_unverified`
  - `test_fingerprint_is_stable_and_changes_with_replacement`
  - `test_no_applyable_findings_returns_none`
  - `test_missing_head_sha_returns_none`

  Expected red: `ImportError` or `AttributeError`.
- **Implement:** pure functions; `hashlib.sha256` over `\0`-joined fields.
- **Green:** `uv run pytest -q`
- **Commit:** `feat(suggestions): build GitHub review payload from applyable fixes`

## Task 8: CLI flag, history v2 and replay (R8 CLI, R10)

**Status:** done.

**Implement:** Opus @ medium / Sonnet @ high · **Review:** Opus @ high / Opus @ medium

- **Files:** modify `src/tri_review/cli.py`, `src/tri_review/history.py`; tests `tests/test_cli.py`, `tests/test_history.py`.
- **Consumes:** `build_review_payload`; `synthesis` from `_stream_graph`; `_repo_identity` for `link_repo`.
- **Produces:**
  - `--review-output PATH`
  - `_write_review_output(findings, head_sha, path)`, which prints the R8 summary line
  - `_stream_graph` returns `synthesis`
  - `RunRecord.synthesis: list[NormalizedFinding]`
  - `SCHEMA_VERSION = 2`
- **Red:**
  - `test_review_output_written_when_a_fix_is_applyable`
  - `test_review_output_not_written_and_existing_file_untouched_when_none_applyable`
  - `test_review_output_not_written_when_synthesis_failed`
  - `test_summary_line_counts_applyable_and_reasons`
  - `test_link_repo_reaches_initial_state`
  - `test_replay_writes_review_output_from_stored_synthesis`
  - `test_v1_history_record_is_a_miss`
  - `test_synthesis_round_trips_through_history`

  Expected red: Click rejects the unknown option, or the field is missing.
- **Implement:** the write follows `_write_output`'s pattern; an `OSError` is a yellow warning, never a failed run.
- **Green:** `uv run pytest -q`
- **Commit:** `feat(cli): --review-output and stored synthesis for replay`

## Task 9: Action posting (R9)

**Implement:** Opus @ high / Opus @ xhigh · **Review:** Opus @ xhigh / Opus @ max

This task mutates GitHub state and handles token scopes. Review it as
security-sensitive: the comment-marker spoofing guard in `pr_comment.sh`
applies here too.

- **Files:** create `scripts/pr_review.sh`, `tests/test_pr_review.py`; modify `action.yml`, `tests/test_action_inputs.py`.
- **Consumes:** `review.json` from Task 8; `resolve_bot_login` (source `pr_comment.sh` as `review_cap.sh` does); the env vars `GH_TOKEN`, `REPO`, `PR_NUMBER`, `REVIEW_FILE`.
- **Produces:**
  - the "Post inline suggestions" step
  - the `suggestions` input (default `'true'`)
  - the `review-path` and `suggestions-posted` outputs
- **Red** (stub `gh` recording calls, as in `tests/test_pr_comment.py`):
  - `test_posts_one_review_with_all_comments`
  - `test_already_posted_fingerprints_are_skipped`
  - `test_nothing_left_posts_nothing_and_says_so`
  - `test_fingerprints_from_other_users_are_ignored`: the spoofing guard.
  - `test_422_falls_back_to_per_comment_posts_with_commit_id`
  - `test_one_failed_fallback_comment_warns_and_continues`
  - `test_403_warns_and_exits_zero`
  - `test_writes_suggestions_posted_output`
  - `test_suggestions_input_is_optional_and_defaults_true`, in `test_action_inputs.py`
  - `test_step_skipped_without_review_path`: parse `action.yml` and assert on the step's `if:`.

  Expected red: the script does not exist.
- **Implement:**
  - `set -uo pipefail` (no `-e`; every failure path is handled).
  - Build JSON with `jq`, never by string splicing.
  - Use `gh api --input -`.
  - Clear `review.json` before the run step, as `report.md` is.
- **Green:** `uv run pytest -q`
- **Commit:** `feat(action): post inline suggested changes, deduped by fingerprint`

## Task 10: README and skill docs

**Implement:** Sonnet @ medium / Opus @ low · **Review:** Sonnet @ medium / Opus @ low

- **Files:** modify `README.md` (Sample output, Inputs and Outputs tables, a "Suggested changes" section with the fork-PR caveat, the `--review-output` flag); modify `.claude/skills/tri-review/SKILL.md` (relay F-IDs; when the user asks to apply `Fn`, use its `file:start-end` and code block, and change nothing unasked).
- **Check:** no test covers the README, so verify by hand that every `action.yml` input and output has a row (`uv run python -c "import yaml; print(sorted(yaml.safe_load(open('action.yml'))['inputs']))"` against the Inputs table), then run `uv run pytest -q`.
- **Commit:** `docs: actionable findings and inline suggestions`

---

## PRD coverage

| Requirement | Task(s) |
|---|---|
| R1 Line-numbered context | 1 |
| R2 Finding schema | 2 |
| R3 Structured synthesis | 3 (schema), 6 (call) |
| R4 Tag enforcement | 3 |
| R5 IDs and order | 3 |
| R6 Report layout | 5 |
| R7 Applyable fixes | 4 |
| R8 Review payload | 7 (builder), 8 (CLI) |
| R9 Action posting | 9 |
| R10 History and replay | 8 |
| R11 Fallback and per-model render | 5, 6 |
| AC6 live check | Pre-merge, below |
| Rollout: bump CI pin | Follow-up PR after merge, below |

## Pre-merge and rollout

1. **One pre-merge review** for the branch: tri-review in CI on the PR
   (default panel), *or* `/adversarial-review` at Opus @ high, not both.
   Reproduce or refute each of its Blocking and major findings with a live
   check or a failing test.
2. **AC6 live check** (Opus @ medium): open a throwaway PR on this repo with
   a planted bug inside a hunk. Run `tri-review --pr <n> --review-output
   review.json`, post it with `REVIEW_FILE=review.json bash
   scripts/pr_review.sh`, apply with **Commit suggestion**, and confirm the
   resulting diff. Include a second planted fix whose replacement contains
   ```` ``` ```` to confirm the lengthened suggestion fence (Q4) applies too.
3. **After merge**, as a separate PR: bump the pinned SHA in
   `.github/workflows/tri-review.yml` to the merge commit, per that file's
   rule that only SHAs on `main` may be pinned.

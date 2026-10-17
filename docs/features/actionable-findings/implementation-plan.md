# Actionable Findings Implementation Plan

**Goal:** Every tri-review finding states its problem, trigger, and fix, points at a line range checked against the PR head, and can be applied on GitHub: by a suggestion click in phase 1, or by a `/tri-review apply` comment in phase 2.

**Architecture:** Reviewers see a payload with numbered lines and return the v2 `Finding` schema. A new anchoring step checks each finding's range against the review context. The synthesizer returns structured `SynthesisOutput` that cites reviewer refs, and code derives consensus, severity, tags, and IDs from those refs. Code also renders the report and the findings JSON. Inline suggestions and the phase 2 apply tool are plain `gh api` clients over that JSON, and neither calls a model.

**Tech Stack:** Python 3.11, Pydantic v2, LangGraph/LangChain (`with_structured_output`), Click, Rich, the `gh` CLI, composite GitHub Actions, Bash (`scripts/pr_comment.sh`), and pytest.

**Spec:** `docs/features/actionable-findings/PRD.md`

## Global Constraints

From `docs/product/PRD.md`:

1. **Never hide a finding.** A finding may be downgraded, labelled unverified, or marked previously dismissed, but it is never silently dropped from the report.
2. **Reviewers stay independent.** Reviewers never see each other's output, the dismissal ledger, or the synthesizer's output.
3. **No PR-authored code runs with secrets.** Nothing tri-review does executes code from the PR under review. Paths, labels, and comment text written by the PR author or commenters are data, never shell.
4. **Spend is visible and avoidable.** Gates (excludes, replay, cache, triage, cap) run before any model call. `--dry-run` never calls a model.
5. **Failures degrade, they don't vanish.** A failed reviewer, synthesizer, or GitHub write is reported in the output. Model calls that already succeeded are never thrown away.
6. **`gh` is the only GitHub client.** All GitHub access goes through the `gh` CLI (`gh api` included). There is no GitHub SDK dependency.
7. **The PR head SHA is the source of truth.** File contents, line numbers, and stored reports are tied to the head SHA the review read.

From the feature PRD:

- Line numbers are on the PR head (the new side), 1-indexed, with inclusive ranges, everywhere: schema, JSON, links, and GitHub comments.
- Inline reviews are always posted with event `COMMENT`.
- Model-written text can't contain a raw `<!--` once rendered (R5.4).

## File Structure

| Path | Action | Responsibility |
|---|---|---|
| `src/tri_review/hunks.py` | create | Parse a unified diff into per-file hunks of new-side lines. Number file text and diff text for the payload |
| `src/tri_review/context.py` | modify | `ReviewContext.render()` emits numbered contents and diff. Keep `hunks` on the context |
| `src/tri_review/schema.py` | modify | `Finding` v2, `Anchor`, `SynthesisFinding`, `SynthesisOutput`, `ReportFinding`. Update `render_findings_md` |
| `src/tri_review/nodes.py` | modify | New `REVIEW_PROMPT` and `SYNTHESIS_PROMPT`. Anchoring in `make_review_node`. Structured `_synthesize`. Raw fallback via `synthesis.fallback` |
| `src/tri_review/anchors.py` | create | `normalize`, `locate`, `anchor`, `attach` (R3) |
| `src/tri_review/synthesis.py` | create | Reviewer refs payload, `derive` (R4.4), `fallback` (R4.6), `assign_ids` (R4.5) |
| `src/tri_review/report.py` | create | `render` (R5), `escape_text`, `fence`, `findings_document` (R6.1), `hidden_block` (R9.5) |
| `src/tri_review/state.py` | modify | Add `findings: list` |
| `src/tri_review/history.py` | modify | `SCHEMA_VERSION = 2`, `RunRecord.findings` |
| `src/tri_review/github.py` | modify | `api(method, path, payload=None, paginate=False)` JSON helper; `bot_login()` |
| `src/tri_review/suggestions.py` | create | `eligible`, `fingerprint`, `comment_body`, `post` (R7) |
| `src/tri_review/cli.py` | modify | `--findings-json`, `--suggest`, `--max-suggestions`. Replay writes and posts findings. Summary counts |
| `src/tri_review/apply.py` | create | `parse_command`, `decode_block`, `plan_edits`, `apply_edits` (pure). `run` (GitHub I/O). `main` (Click) |
| `pyproject.toml` | modify | `tri-review-apply = "tri_review.apply:main"` |
| `action.yml` | modify | Inputs `inline-suggestions` and `max-suggestions`. Output `findings-path`. Pass the flags. Hand `FINDINGS_FILE` to the comment step |
| `scripts/pr_comment.sh` | modify | Append the hidden findings block, with the size guard |
| `apply/action.yml` | create | Composite action that runs `tri-review-apply` |
| `scripts/bench.py` | create | Recall benchmark (R10) |
| `bench/cases.toml` | create | Benchmark cases |
| `bench/results/baseline.md`, `bench/results/after-prompt.md` | create | Benchmark outputs |
| `README.md`, `.claude/skills/tri-review/SKILL.md` | modify | New report format, flags, inputs, apply workflow, and the synthesizer structured-output caveat |
| `tests/test_hunks.py`, `tests/test_anchors.py`, `tests/test_derive.py`, `tests/test_report.py`, `tests/test_suggestions.py`, `tests/test_apply.py`, `tests/test_bench.py` | create | Unit tests per module |
| `tests/test_context.py`, `tests/test_render.py`, `tests/test_nodes.py`, `tests/test_synthesizer.py`, `tests/test_graph.py`, `tests/test_history.py`, `tests/test_cli.py`, `tests/test_action_inputs.py`, `tests/test_pr_comment.py`, `tests/test_github.py` | modify | Cover the changed behaviour |

Test conventions to reuse:

- `gh` is stubbed by monkeypatching `github._run` (`tests/test_github.py::_recorder`).
- Shell scripts are run against a stub `gh` on `PATH` (`tests/test_pr_comment.py::_run`).
- LLMs are stubbed with objects that have `invoke` (`tests/test_synthesizer.py::CapturingLLM`).

From Task 6 on, the stubs also need `with_structured_output(schema, include_raw=True)` returning an object whose `invoke` gives `{"parsed": ..., "raw": ..., "parsing_error": None}`.

Red/green commands use `uv run pytest -q <path>`. Every task ends with `make test` green before its commit.

---

## Phase 1: v1.3.0

### Task 1: Recall benchmark harness and baseline

**PRD:** R10.1–R10.5, AC 4.

- **Create:**
  - `scripts/bench.py`
  - `bench/cases.toml`
  - `tests/test_bench.py`
- **Create later (the run step below):** `bench/results/baseline.md`
- **Consumes:** `context.build_context(diff, reader=...)`, `nodes.review_with(model, payload, use_cache=False)`, and `git`.
- **Produces:**
  - `bench.ground_truth(reverse_diff: str) -> dict[str, set[int]]`
  - `bench.is_hit(finding, truth: dict[str, set[int]], slack: int = 3) -> bool`
  - `bench.finding_range(finding) -> tuple[int, int] | None`: the anchored range if `finding.anchor` exists, else `line`..`end_line or line`. Read with `getattr`, so it works on v1 findings.
  - `bench.run_case(case, reviewers) -> list[ReviewResult]`: builds the reverse diff and its context, then calls `review_with` once per reviewer.
  - `bench.table(rows) -> str`
  - CLI: `uv run python scripts/bench.py --reviewer SPEC ... --cases bench/cases.toml --out FILE`

Steps:

1. **Red.** In `tests/test_bench.py`, write tests:
   - `ground_truth` of a reverse diff that adds lines 10–11 and removes a run before new line 20 returns `{"src/x.py": {10, 11, 20}}`.
   - `is_hit` is true at ±3 lines, false at ±4, and false on another file.
   - `finding_range` works on a v1 `Finding` with `line=None`, giving `None`.
   - `table` renders per-reviewer, panel, and consensus recall, findings per case, and median words. It shows "anchored %" as `n/a` when no finding has an anchor.

   `uv run pytest -q tests/test_bench.py` → fails with `ModuleNotFoundError: bench`. Import it via `importlib` from `scripts/`.
2. **Green.** Implement the pure functions. The runner shells `git diff <fix> <fix>^ -- src/` and `git show <fix>^:<path>` per file, and builds a reader over those. Words per finding are counted over `title`, `problem`/`detail`, `trigger`, and `fix`/`suggested_fix`, whichever exist. `uv run pytest -q tests/test_bench.py` → passes.
3. **Write `bench/cases.toml`** with the PRD Open Question 3 candidates. Keep only those where `git diff <fix> <fix>^ -- src/` is non-empty.
4. **Commit:** `bench: measure reviewer recall on reverted fix commits`
5. **Run the baseline.** This is a manual, paid run, on this commit, before any prompt or schema change. The panel matches this repo's CI default: `gpt-6-sol@high gpt-5.6-sol@high claude-sonnet-5`. Run `uv run python scripts/bench.py --reviewer ... --out bench/results/baseline.md`.
6. **Commit:** `bench: record baseline recall`

### Task 2: Numbered payload and diff hunk parser

**PRD:** R2.1, R2.2, R2.5, and the R3.2/R3.5 inputs.

- **Create:**
  - `src/tri_review/hunks.py`
  - `tests/test_hunks.py`
- **Modify:**
  - `src/tri_review/context.py`
  - `tests/test_context.py`
- **Produces:**
  - `@dataclass(frozen=True) class Hunk: path: str; lines: dict[int, str]`: the new-side line → text, for added and context lines.
  - `hunks.parse(diff) -> dict[str, list[Hunk]]`. Paths are decoded with `gating.diff_path(..., "b/")`. A `+++ /dev/null` file is omitted.
  - `hunks.number_lines(text) -> str`
  - `hunks.number_diff(diff) -> str`
  - `ReviewContext.hunks` (a cached property over `diff`).
  - `ReviewContext.render()` returns the numbered output.

Steps:

1. **Red.** Write tests:
   - `parse` covers new-side numbering across two hunks, a pure addition, a deletion-only hunk (no new-side lines from its `-` lines), a quoted path, and `\ No newline at end of file`.
   - `number_lines` right-aligns to the widest number. A 9-line and a 10-line file pad differently.
   - `number_diff` leaves headers untouched, and pads `-` lines to the same width.
   - In `test_context`, `render()` contains `   1 | ` for file contents and numbered diff lines. `estimated_tokens` is greater than the unnumbered estimate.

   `uv run pytest -q tests/test_hunks.py tests/test_context.py` → fails.
2. **Green.** Implement. `render()` numbers each file body and the diff. Budget logic is unchanged, because `estimated_tokens` already measures `render()`. Note that `build_context` estimates per-file `cost` on raw content. Change it to `config.estimate_tokens(hunks.number_lines(content))`, so the budget check matches what's rendered, and add a test for that. → green.
3. **Commit:** `context: number file and diff lines in the review payload`

### Task 3: Finding schema v2

**PRD:** R1.1–R1.5, R5.5.

- **Modify:**
  - `src/tri_review/schema.py`
  - `src/tri_review/nodes.py` (`_raw_listing` only)
  - `tests/test_render.py`
  - `tests/test_nodes.py`
  - `tests/test_synthesizer.py` (the `_result` helper uses `problem`/`trigger`/`fix`)
- **Produces:**
  - `Finding` with `file`, `line`, `end_line`, `severity`, `category`, `title`, `problem`, `trigger`, `fix`, `original_code`, `replacement_code`, plus `anchor: SkipJsonSchema[Anchor | None] = None`.
  - `Anchor(status: Literal["verified","relocated","unverified"], line: int | None, end_line: int | None, in_diff: bool)`.

Steps:

1. **Red.** Write tests:
   - A finding missing `title` gets one from `problem`'s first sentence.
   - A missing or blank `trigger` validates, with `trigger == ""`.
   - Missing `problem` or `fix` → set aside as malformed (the existing `_set_aside_malformed` path).
   - `ReviewOutput.model_json_schema()` contains `trigger` and `replacement_code` and doesn't contain `anchor`.
   - `render_findings_md` shows **Problem:**, **Trigger:** ("Trigger not stated" when blank), and **Fix:**, plus a `diff` block when both code fields are present.
   - `_raw_listing` shows the same three labels.

   → fails.
2. **Green.** Change the schema:
   - Rename `_title_from_detail` → `_title_from_problem`.
   - Add a before-validator that defaults `trigger` to `""`.
   - Update `render_findings_md` and `_raw_listing` (temporary; Task 7 replaces it).
   - Field descriptions carry the sentence limits (R1.5).

   → green.
3. **Commit:** `schema: findings state problem, trigger and fix, with an exact code range`

### Task 4: Reviewer prompt rewrite

**PRD:** R2.3, R2.4, G1.

- **Modify:**
  - `src/tri_review/nodes.py` (`REVIEW_PROMPT`)
  - `src/tri_review/providers.py` (the prompt-caching comment only)
  - `tests/test_nodes.py`

Steps:

1. **Red.** Add tests asserting the prompt:
   - Names the line-number prefix and says it's not part of the code.
   - Requires a trigger.
   - States the three-level severity rubric.
   - Excludes style, docs, and logging suggestions.
   - Limits `replacement_code` to one contiguous range.
   - Still contains the "empty findings list is a strong claim" rule.

   These follow the pattern of the existing prompt tests in `tests/test_synthesizer.py` (for example `test_blocking_requires_evidence_not_just_agreement`). → fails.
2. **Green.** Rewrite `REVIEW_PROMPT` per R2.4. Then re-measure tool schema + `REVIEW_PROMPT` tokens (`config.estimate_tokens` over `json.dumps(ReviewOutput.model_json_schema())` plus the prompt), and update the "~460 tokens" reasoning in the prompt-caching comment in `providers.build_llm` (R2.6). → green.
3. **Commit:** `nodes: ask reviewers for triggered, line-anchored findings`

### Task 5: Anchors, and the benchmark comparison

**PRD:** R3.1–R3.7, R10.3–R10.5, G2, AC 2, AC 4.

The after-prompt benchmark runs here rather than in Task 4, so it can score hits on anchored ranges and report the anchored share (R10.4).

- **Create:**
  - `src/tri_review/anchors.py`
  - `tests/test_anchors.py`
- **Modify:**
  - `src/tri_review/nodes.py` (`make_review_node`)
  - `tests/test_graph.py`
  - `scripts/bench.py` (call `anchors.attach` on each result)
  - `tests/test_bench.py`
- **Create:** `bench/results/after-prompt.md`
- **Consumes:** `ReviewContext.files`, `ReviewContext.hunks`, and `Finding`.
- **Produces:**
  - `anchors.normalize(code: str) -> list[str]`
  - `anchors.source_for(path, ctx) -> dict[int, str] | None`
  - `anchors.locate(original: str, source: dict[int, str], near: int | None) -> tuple[int, int] | None`: steps 3–4 of R3.4. The phase 2 apply tool reuses it.
  - `anchors.anchor(finding, source, file_hunks) -> Anchor`
  - `anchors.attach(result: ReviewResult, ctx) -> ReviewResult`: returns a copy, with each finding's `anchor` set.
  - `anchors.suggestable(f) -> bool`
  - `anchors.applicable(f) -> bool`

Steps:

1. **Red.** In `tests/test_anchors.py`, write one test for each R3.4 step:
   - No line, with `original_code` found once (`relocated`) and found twice (`unverified`).
   - No `original_code`.
   - No source.
   - An exact match (`verified`).
   - A CRLF and trailing-space match (`verified`).
   - A unique match elsewhere (`relocated`).
   - Two matches with one nearer (`relocated` to it).
   - Two equidistant matches (`unverified`).
   - No match (`unverified`).

   Then:
   - Source from the hunks when the file is in `ctx.dropped`.
   - `in_diff` true for the first and last new-side line of a hunk, false one line outside, and false for a range spanning two hunks.
   - `suggestable`/`applicable` truth tables.

   In `tests/test_graph.py`, a cached `ReviewResult` (with `cached=True`) still comes out of the node with anchors set. → fails.
2. **Green.** Implement. In `make_review_node`, wrap `review_with(...)` with `anchors.attach(result, state["context"])` when `state.get("context")` is present. Anchors are never written to the reviewer cache, because `attach` runs after `cache.save` and returns a copy. → green.
3. **Commit:** `anchors: check each finding's line range against the PR head`
4. **Red/green for the bench script.** A test that `bench.run_case` attaches anchors, so `finding_range` uses the anchored range. Then make `run_case` call `anchors.attach(result, ctx)`, guarded by an import check so the script still runs at the baseline commit. → green.
5. **Commit:** `bench: score hits on anchored ranges`
6. **Run the benchmark** (manual, paid) with the same panel and cases as the baseline, writing `bench/results/after-prompt.md`. If panel recall is below the baseline, stop and revise `REVIEW_PROMPT` (Task 4) before continuing (AC 4).
7. **Commit:** `bench: record recall after the reviewer prompt and payload changes`

### Task 6: Derivation from structured synthesis

**PRD:** R4.2–R4.6, AC 3.

- **Create:**
  - `src/tri_review/synthesis.py`
  - `tests/test_derive.py`
- **Modify:** `src/tri_review/schema.py`
- **Produces:**
  - `SynthesisFinding` and `SynthesisOutput(findings: list[SynthesisFinding])`, with the R4.3 fields.
  - `ReportFinding` with the R6.1 fields except `suggestable`/`applicable`, which are computed properties.
  - `synthesis.refs(succeeded) -> tuple[str, dict[str, tuple[str, Finding]]]`: the JSON payload and a ref map from `"A1"` to `(model, finding)`.
  - `synthesis.derive(output, ref_map, panel_order) -> tuple[list[ReportFinding], list[str]]`: the findings (IDs assigned) and the notes (unknown refs dropped, N appended).
  - `synthesis.fallback(succeeded) -> list[ReportFinding]`
  - `synthesis.assign_ids(findings) -> list[ReportFinding]`

Steps:

1. **Red.** In `tests/test_derive.py`, test that:
   - `refs` letters reviewers in panel order and numbers their findings from 1, carrying each anchor.
   - Two refs from one model → `consensus` is false.
   - Refs from two models on the same file → true. Refs from two models on different files → false.
   - Severity is the source maximum, whatever the synthesizer said.
   - The primary source prefers `verified` + `in_diff` over an earlier `unverified` source.
   - `blocking` on a single-model finding → `optional`.
   - `blocking` with a minor severity → `optional`.
   - `blocking` with a blank trigger or a blank `falsified_by` → `optional`.
   - `blocking` with `check` set → `blocking_pending_check`.
   - `blocking_pending_check` without `check` → `optional`.
   - A dismissed finding without `new_evidence` → `optional`. With it, the tag is kept.
   - A synthesizer-added finding (`sources=[]`) → `optional`, `minor`.
   - An unknown ref is dropped and noted.
   - Every uncited reviewer finding is appended as `optional`, and the note gives the count.
   - The ID order follows R4.5.
   - `fallback` yields one `optional` finding per reviewer finding.

   → fails.
2. **Green.** Implement as pure functions with no LLM calls. → green.
3. **Commit:** `synthesis: derive consensus, severity and tags in code from cited reviewer findings`

### Task 7: Structured synthesizer and code-rendered report

**PRD:** R4.1, R4.6, R5.1–R5.4, AC 1, AC 9 (render half).

- **Create:**
  - `src/tri_review/report.py`
  - `tests/test_report.py`
- **Modify:**
  - `src/tri_review/nodes.py` (`SYNTHESIS_PROMPT`, `_synthesize`, `synthesize_node`, remove `_raw_listing`)
  - `src/tri_review/state.py`
  - `tests/test_synthesizer.py`
- **Consumes:** `synthesis.*`, the existing header helpers (`_panel_line`, `_failure_note`, …), and the repo and head SHA from state (`repo`, `head_ref`).
- **Produces:**
  - `report.escape_text(s) -> str`: neutralizes `<!--`, and backtick runs that could close an enclosing fence.
  - `report.fence(code, lang) -> str`
  - `report.render(findings, header, notes, repo, head_sha, dismissals) -> str`
  - `synthesize_node` returns `{"final_report", "synthesized", "findings"}`.

Steps:

1. **Red.** In `tests/test_report.py`, test that:
   - The summary line counts each tag.
   - Consensus and Unique sections split on `consensus`.
   - A Unique finding names its model followed by "(unverified by the other reviewers)".
   - A finding header has the R5.3 shape.
   - The location is a link when the repo and head are set, and plain code text otherwise.
   - An unverified location shows "(line not verified)".
   - The diff block appears only when the anchor is verified or relocated.
   - An empty `replacement_code` renders only `-` lines.
   - A replacement containing ```` ``` ```` gets a 4-backtick fence.
   - A `problem` containing `<!-- tri-review-findings:v1 x -->` renders with no raw `<!--` (AC 9).
   - "Actionable Next Steps" has one line per ID.
   - "Previously dismissed" appears with the reason.

   In `tests/test_synthesizer.py`, convert `CapturingLLM` to support `with_structured_output`, and test that:
   - The structured output flows into the rendered report.
   - Recorded dismissals still reach only the synthesizer.
   - Reviewers never receive the ledger (keep the existing test).
   - A failed synthesis call gives `synthesized=False`, a "Synthesis failed" note, and `fallback` findings rendered with the previously-rejected list.
   - A failed parse (`parsing_error`) is treated as a failure, the same way as in `review_with`.

   Remove the prompt-only tests whose rules are now enforced in code (severity labels, Blocking evidence), replacing them with derive tests already written in Task 6. Keep a prompt test that asks for `falsified_by` and `check`. → fails.
2. **Green.** Rewrite `SYNTHESIS_PROMPT`:
   - Cite refs.
   - Merge by underlying problem.
   - Tag rules, now stated as guidance.
   - The dismissal handling as today.
   - Problem, Trigger, and Fix limits.

   Then `_synthesize` calls `llm.with_structured_output(SynthesisOutput, include_raw=True)`, runs `derive`, and renders the report. Add `findings` to `ReviewState`. → green.
3. **Commit:** `synthesis: return structured findings and render the report in code`

### Task 8: Findings JSON, history v2 and replay

**PRD:** R6.1, R6.2, R8.1, R8.2, AC 8.

- **Modify:**
  - `src/tri_review/history.py`
  - `src/tri_review/report.py`
  - `src/tri_review/cli.py`
  - `tests/test_history.py`
  - `tests/test_cli.py`
- **Produces:**
  - After each report, the CLI prints `N findings · S suggestable · A applicable` (the Observability requirement).
  - `report.findings_document(findings, repo, pr, head_sha, synthesized) -> dict` (R6.1 `version: 1`).
  - `RunRecord.findings: list[dict]`.
  - `history.SCHEMA_VERSION = 2`.
  - CLI `--findings-json FILE`.
  - `_stream_graph` returns `(report, results, synthesized, findings)`.

Steps:

1. **Red.** Write tests:
   - In `test_history`: a v2 record round-trips its `findings`, and a stored `"schema": 1` file loads as `None`.
   - In `test_cli`: `--findings-json` writes R6.1 after a fresh run, after a replay (same JSON as the original), and after a synthesis fallback (`"synthesized": false`).
   - An unwritable path warns and exits 0.
   - The fields `suggestable` and `applicable` match `anchors.suggestable`/`applicable`.

   → fails.
2. **Green.** Implement. `_write_findings(doc, path)` mirrors `_write_output`. → green.
3. **Commit:** `cli: write findings as JSON and keep them in history for replay`

### Task 9: Inline suggestions

**PRD:** R7.1–R7.7, AC 5.

- **Create:**
  - `src/tri_review/suggestions.py`
  - `tests/test_suggestions.py`
- **Modify:**
  - `src/tri_review/github.py`
  - `src/tri_review/cli.py`
  - `tests/test_github.py`
  - `tests/test_cli.py`
- **Consumes:** `ReportFinding` dicts from the findings document, `github.api`, and `github.bot_login`.
- **Produces:**
  - `github.api(method: str, path: str, payload: dict | None = None, paginate: bool = False) -> Any`. This runs `gh api -X METHOD path [--paginate] --input -`, with the payload on stdin. Never interpolate the payload into argv. It raises `github.ApiError(status: int | None, message)` on a non-zero exit, parsing the HTTP status from `gh`'s stderr (`HTTP 422`).
  - `github.bot_login() -> str`: the same rule as `resolve_bot_login`.
  - `suggestions.eligible(findings, mode, cap) -> tuple[list[dict], int]`, where the int is the number capped.
  - `suggestions.fingerprint(f) -> str`
  - `suggestions.comment_body(f) -> str`
  - `suggestions.review_comment(f) -> dict`
  - `suggestions.post(repo, pr, head_sha, findings, mode, cap) -> PostResult(posted: int, deduped: int, capped: int, failed: list[str])`.
  - CLI flags `--suggest [none|consensus|all]` (default `none`) and `--max-suggestions N` (default 15).

Steps:

1. **Red.** In `test_suggestions.py`, test that:
   - `eligible` with `consensus` excludes single-model and non-suggestable findings, `all` includes single-model ones, and the cap keeps ID order and counts the overflow.
   - `review_comment` has a single-line range with no `start_line` and a multi-line range with `start_line`, `start_side`, `line`, and `side`.
   - `comment_body` starts with the marker, includes a `suggestion` fence longer than the inner backticks, and gives an empty suggestion block for an empty replacement.
   - `fingerprint` changes with path, anchored range, or `original_code`, and doesn't change with `replacement_code` or trailing whitespace.
   - `post` lists existing comments filtered by `bot_login` and skips matching fingerprints.
   - A run with everything deduped makes no POST.
   - A 422 on the review falls back to per-comment POSTs and records the ones that fail.
   - A 403 posts nothing and records it in `failed`.
   - The payload goes through stdin, never argv.
   - A comment body over 65,536 characters is skipped and noted.

   In `test_github.py`, test that `api` sends JSON on stdin and parses `HTTP 422`.

   In `test_cli.py`, test that:
   - `--suggest consensus` calls `suggestions.post` with the run's findings and head SHA.
   - A `working_tree_reason` refuses with a message and doesn't post.
   - With no repo identity, it refuses.
   - `--dry-run` never posts.
   - Skips (path, triage, empty) never post.
   - A replay posts (dedup makes it a no-op).
   - A posting failure leaves the exit code 0 and prints to stderr.

   → fails.
2. **Green.** Implement. `github.api` uses `subprocess.run` with `input=`. `_run` takes no stdin today, so add an `input` keyword to `_run`, keeping the existing test monkeypatch signature compatible with `def fake_run(args, input=None)`. Update `_recorder` in the tests to accept it. → green.
3. **Commit:** `suggestions: post consensus fixes as GitHub suggested changes`

### Task 10: Action inputs and outputs for phase 1

**PRD:** R7.1, R7.2, and Observability (`findings-path`).

- **Modify:**
  - `action.yml`
  - `tests/test_action_inputs.py`

Steps:

1. **Red.** Write tests:
   - `inline-suggestions` (default `consensus`) and `max-suggestions` (default `15`) are declared.
   - The run script turns them into `--suggest` and `--max-suggestions`.
   - `--findings-json "$RUNNER_TEMP/tri-review/findings.json"` is always passed. It's cleared first, like `REPORT`.
   - `findings-path` is output only when the file exists.
   - An invalid `inline-suggestions` value fails the step with a message.

   `test_every_new_input_is_declared` covers the declaration. → fails.
2. **Green.** Implement in `action.yml`, passing values through `env` as the existing inputs do. → green.
3. **Commit:** `action: post inline suggestions and expose findings-path`

### Task 11: Phase 1 docs

**PRD:** Rollout (v1.3.0) and the structured-output caveat in Rules.

- **Modify:**
  - `README.md` (report format, the new flags table rows, the new inputs/outputs rows, an "Applying fixes" subsection for suggestions, the synthesizer structured-output requirement, and replacing the "Actionable Next Steps" description)
  - `.claude/skills/tri-review/SKILL.md` (Step 5: the report now has IDs and Problem/Trigger/Fix. Relay it verbatim, as before)
- **Tests:** none (docs). Check `make test` is green.
- **Commit:** `docs: describe the per-finding report and inline suggestions`

Release gate for v1.3.0:

- `bench/results/after-prompt.md` meets AC 4.
- The live checks (AC 1 and AC 6) are run with the CLI from the implementing branch, against a PR in this repo: `uv run tri-review --repo JairoE/tri-review --pr <n> --suggest all`. They can't run through this repo's CI before merge. CI runs the action pinned to a SHA on main, and pinning a PR-branch SHA is exactly what `.github/workflows/tri-review.yml` forbids.
- After merge, bump that pin to the merge commit, as #41 did, so CI reviews with the new format.

---

## Phase 2: v1.4.0

### Task 12: Apply core (pure)

**PRD:** R9.2, R9.5, R9.6, and the trailing-newline and empty-replacement edge cases.

- **Create:**
  - `src/tri_review/apply.py` (pure part)
  - `tests/test_apply.py`
- **Consumes:** `anchors.locate` and `anchors.normalize`.
- **Produces:**
  - `apply.parse_command(body) -> list[str] | None`: the IDs, or `None` for a usage error.
  - `apply.decode_block(comment_body) -> dict | None`: reads only the last non-blank line.
  - `apply.plan_edits(requested, block, files: dict[str, str]) -> tuple[list[Edit], dict[str, str]]`, where `Edit(path, start, end, replacement)`. The dict maps each refused ID to its reason.
  - `apply.apply_edits(text, edits) -> str`: bottom-up.

Steps:

1. **Red.** Write tests:
   - **Grammar:** `/tri-review apply F1 F3`, `/TRI-REVIEW APPLY f1,f3`, and leading/trailing spaces all parse. `/tri-review apply` with no IDs, `/tri-review apply X1`, and a command on a later line give `None`.
   - **Block:** found only on the last line. A block string in the middle of the body is ignored (AC 9). Bad base64 or JSON → `None`.
   - **Planning:** the R9.6 reasons, verbatim. The exact location and a relocated location are both accepted. Two overlapping requests → the later one is refused.
   - **Applying:** two edits in one file applied bottom-up; an empty replacement deletes; a replacement without a trailing newline doesn't merge into the next line; a CRLF file keeps CRLF.

   → fails.
2. **Green.** Implement. → green.
3. **Commit:** `apply: plan and apply reviewed fixes from a report's findings block`

### Task 13: Findings block in the report comment

**PRD:** R9.5, and the comment-size rule.

- **Modify:**
  - `src/tri_review/report.py` (`hidden_block(document) -> str`)
  - `scripts/pr_comment.sh`
  - `action.yml` (pass `FINDINGS_FILE`)
  - `tests/test_report.py`
  - `tests/test_pr_comment.py`

Steps:

1. **Red.** Write tests:
   - `hidden_block` contains only applicable findings, with the R9.5 fields, and `apply.decode_block` round-trips it.
   - With `FINDINGS_FILE` set, the posted body's last line is the block.
   - When the body plus the block is over 65,000 characters, the block is left out and a visible note is added.
   - With no `FINDINGS_FILE` the body is unchanged.
   - `comment_header` parsing (cap tally) is unaffected.

   → fails.
2. **Green.** The block line is produced by a small CLI entry so Bash never builds JSON: `python -m tri_review.report --hidden-block FILE`. `pr_comment.sh` appends its output after the attribution footer. → green.
3. **Commit:** `action: embed applicable fixes in the report comment for /tri-review apply`

### Task 14: Apply GitHub I/O and console script

**PRD:** R9.1, R9.3, R9.4, R9.7–R9.9, AC 7.

- **Modify:**
  - `src/tri_review/apply.py` (add `run` and `main`)
  - `pyproject.toml`
  - `tests/test_apply.py`
- **Consumes:** `github.api`, `github.bot_login`, `github.fetch_file_content`, and the comment-kind rules from `scripts/pr_comment.sh::count_reviews`, ported to Python as `apply.report_kind(body)` with tests mirroring `tests/test_pr_comment.py`'s kind cases.
- **Produces:**
  - `apply.run(repo, pr, comment_id, actor, body) -> int`
  - Console script `tri-review-apply`

Steps:

1. **Red.** With a stubbed `github.api`, write tests:
   - A 👀 reaction is posted first.
   - Actor permission `read` → refusal reply, no commit.
   - A fork PR (`head.repo.full_name != base.repo.full_name`) → the fork reply.
   - No report comment by the bot → the "no report" reply.
   - The newest bot comment is a cap note or a skip, and an older report has a block → the older report is used. Cap notes and skips don't replace the last report (R9.5).
   - The newest report has no block → the "no applicable fixes in the latest report" reply, never an older report's block.
   - A file under `.github/workflows/` and an unreadable file are each refused with the R9.6 reasons.
   - An executable file (`100755` in the head tree) keeps mode `100755` in the new tree.
   - A report comment by another user with a valid block is ignored.
   - The happy path: blobs → tree (base `head.sha`'s tree) → commit with the R9.7 message and parent `head.sha` → `PATCH refs/heads/<head.ref>` with `force: false` → a reply listing applied IDs and the commit link, plus the re-review note.
   - A 422 on the ref update → the "branch moved" reply, exit 0.
   - A failure on the reply POST → exit 1.
   - A usage error → the usage reply, exit 0.
   - Missing `--repo` → exit 2.

   → fails.
2. **Green.** Implement, and add `tri-review-apply = "tri_review.apply:main"` to `[project.scripts]`. → green.
3. **Commit:** `apply: commit requested fixes to the PR branch from a comment command`

### Task 15: Apply action and docs

**PRD:** R9.10, Rollout (v1.4.0).

- **Create:** `apply/action.yml`
- **Modify:**
  - `README.md` (an "Applying fixes with /tri-review apply" section with the consumer workflow, permissions, and the fork and re-review notes)
  - `tests/test_action_inputs.py` (or a new `tests/test_apply_action.py`)

Steps:

1. **Red.** Write tests:
   - `apply/action.yml` passes the comment body, comment ID, actor, PR number, and repo only through `env`. No `${{ }}` appears inside any `run:` script body.
   - It installs with `--constraint requirements-lock.txt`, as `action.yml` does.
   - The README's workflow snippet has the `issue_comment` trigger, the `if` guard, the three permissions, and a `concurrency` group.

   → fails.
2. **Green.** Implement. → green.
3. **Commit:** `action: add the tri-review apply action for /tri-review apply comments`

Release gate for v1.4.0, run after merge, because `issue_comment` workflows only run from the default branch:

- Add the apply workflow to this repo, pinned to the merge commit's SHA, never `./apply`.
- On a test PR, `/tri-review apply` on one in-diff and one out-of-diff applicable finding gives one commit with exactly those changes.
- A command from an account without write access is refused.

Before merge, the same path can be exercised by running `tri-review-apply` locally against a test PR, with your own `gh` auth.

---

## PRD Coverage

| PRD | Task |
|---|---|
| R1.1–R1.5 | 3 |
| R2.1, R2.2, R2.5 | 2 |
| R2.3, R2.4 | 4 |
| R3.1–R3.7 | 5 |
| R4.1, R4.6 (wiring) | 7 |
| R4.2–R4.5, R4.6 (fallback findings) | 6 |
| R5.1–R5.4 | 7 |
| R5.5 | 3 |
| R6.1, R6.2 | 8 |
| R7.1–R7.7 | 9, 10 |
| R8.1, R8.2 | 8 (replay posting in 9) |
| R9.1, R9.3, R9.4, R9.7–R9.9 | 14 |
| R9.2, R9.6 | 12 |
| R9.5 | 12 (decode), 13 (encode and embed) |
| R9.10 | 15 |
| R10.1–R10.5 | 1 (harness, baseline), 5 (anchored scoring, after-prompt run) |
| Observability counts | 8 (suggestable and applicable counts, appended-findings note), 9 (posted, deduped, capped, failed) |
| Rollout docs | 11, 15 |
| AC 1–9 | 7 / 5 / 6 / 5 / 9 / Task 11 release gate / 14 / 8 / 7 + 12 |

## Model and Effort Routing

- **Tasks 5–7** (anchors, derivation, structured synthesis): Fable or Opus at high effort. These are where the invariants live.
- **Tasks 9, 12, and 14** (`gh api` clients): Opus.
- **Tasks 2, 3, 8, 10, 11, 13, and 15**: well specified enough for a Sonnet subagent, with Opus reviewing the diff.
- The plan touches more than two subsystems (payload, schema, synthesis, CLI, Action, shell), so open the build with `/recon`.
- **One pre-merge review per PR.** This repo's CI already runs tri-review on every PR. Reproduce or refute its Blocking and major findings (a failing test, a live `gh api` check) rather than stacking `/adversarial-review` on top. Use `/adversarial-review` only on a PR where tri-review didn't run. Tasks 13–14 deserve the closest look, because they write to repositories.

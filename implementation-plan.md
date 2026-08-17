# Implementation Plan: `tri-review` CLI

Source spec: `prd.md`. Task checklist for build tooling: `tasks/todo.md`.

## Overview

Build the MVP of `tri-review`: a Python CLI that fetches a real GitHub PR diff via the `gh` CLI, packages it with full-file local context, fans out to three LLM reviewers concurrently through a LangGraph graph, and synthesizes their structured findings into a consensus/unique-insights Markdown report rendered with Rich.

## Architecture Decisions

- **`gh` CLI over PyGithub:** `gh pr diff` and `gh pr view --json` give diff fetching, auth, and branch auto-detection with zero token management. GitHub API integration is deferred to post-MVP (needed only for `--comment`).
- **Structured findings, not free text:** Each reviewer returns JSON conforming to a shared Pydantic `Finding` schema via LangChain's `with_structured_output`. This is what makes cross-model consensus matching reliable and is non-negotiable in the MVP.
- **Failure-tolerant fan-in:** Review nodes never raise; they record success (findings) or failure (error string) into state. The synthesizer guards on ≥2 successes. This keeps LangGraph's parallel superstep from aborting the whole run on one flaky provider.
- **Truncation over chunking:** One token budget (default ~100k tokens, 4 chars/token estimate), dropping full-file contents largest-first with a printed warning. Chunking is post-MVP.
- **Models are config:** IDs live in `config.py` with env overrides. Current provider flagship IDs must be verified against provider docs during Task 4 — do not trust IDs from memory or from older docs.
- **Build order is diff-pipeline-first:** The PR-fetching and context slice lands before any LLM code, with a `--dry-run` flag to verify it against real repos. This inverts the old plan's mistake of mocking the core feature.

## Task List

### Phase 1: Foundation (real data, no LLMs)

---

## Task 1: Project scaffolding and config

**Description:** Create the package skeleton: `pyproject.toml` with a `tri-review` console entry point, dependencies (click, rich, python-dotenv, langgraph, langchain-openai, langchain-anthropic, langchain-google-genai, pytest), `src/tri_review/` layout, `.env.example` with the three provider keys, and `config.py` holding model IDs, token budget, and timeout — each overridable via `TRI_REVIEW_*` env vars.

**Acceptance criteria:**
- [ ] `pip install -e .` succeeds and `tri-review --help` prints usage
- [ ] `config.py` exposes model IDs, token budget (default 100_000), and per-model timeout (default 120s), each overridable via env var
- [ ] `.env.example` documents `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY` — and nothing else (no GITHUB_TOKEN)

**Verification:**
- [ ] `pip install -e . && tri-review --help` in a fresh venv
- [ ] `pytest` runs (zero tests collected is fine)

**Dependencies:** None
**Files likely touched:** `pyproject.toml`, `src/tri_review/__init__.py`, `src/tri_review/config.py`, `src/tri_review/cli.py`, `.env.example`
**Estimated scope:** Small

---

## Task 2: PR fetching via `gh` (preflight + diff + auto-detect)

**Description:** Implement `github.py`: preflight checks (`gh` installed, `gh auth status` ok, cwd is a git repo with a GitHub remote), `fetch_diff(pr_number)` wrapping `gh pr diff`, and `detect_pr()` wrapping `gh pr view --json number` for the no-argument flow. Wire `--pr` (optional) into the CLI. Every failure mode maps to a specific actionable message and non-zero exit.

**Acceptance criteria:**
- [ ] `tri-review --pr <n>` fetches the real diff for that PR in a real repo
- [ ] `tri-review` with no flag resolves the current branch's open PR, and errors clearly when the branch has none
- [ ] Missing `gh`, unauthenticated `gh`, and non-repo cwd each produce a distinct actionable error, not a traceback (PRD acceptance criterion 6)

**Verification:**
- [ ] Unit tests for error mapping with subprocess mocked: `pytest tests/test_github.py`
- [ ] Manual check in a real repo with an open PR: correct diff, correct auto-detection

**Dependencies:** Task 1
**Files likely touched:** `src/tri_review/github.py`, `src/tri_review/cli.py`, `tests/test_github.py`
**Estimated scope:** Small

---

## Task 3: Context builder with token budget

**Description:** Implement `context.py`: parse changed file paths from the unified diff, read each file's full content from the local checkout (skip deleted files — their hunks are already in the diff), assemble the context block, and enforce the token budget by dropping full-file contents largest-first (keeping their diff hunks) until under budget, emitting a warning that names the dropped files. Add `--dry-run` to the CLI: print diff stats, included/truncated files, and estimated tokens, then exit before any LLM work.

**Acceptance criteria:**
- [ ] Changed-file paths are parsed correctly from unified diff headers (added, modified, deleted, renamed)
- [ ] Over-budget input drops largest files first and prints a warning naming them (PRD acceptance criterion 5)
- [ ] `tri-review --pr <n> --dry-run` shows the full context summary without touching any API key

**Verification:**
- [ ] Unit tests for diff parsing and the truncation policy: `pytest tests/test_context.py`
- [ ] Manual check: `--dry-run` against a real PR shows correct files and plausible token counts

**Dependencies:** Task 2
**Files likely touched:** `src/tri_review/context.py`, `src/tri_review/cli.py`, `tests/test_context.py`
**Estimated scope:** Medium

---

### Checkpoint: Foundation
- [ ] `pytest` green, `tri-review --pr <n> --dry-run` works against a real PR with no API keys set
- [ ] The old plan's core gap is closed: real diff and real context, verified before any LLM code exists

### Phase 2: Review pipeline

---

## Task 4: Findings schema, graph state, and one working reviewer

**Description:** Define the Pydantic models in `schema.py` (`Finding`: file, line (nullable), severity, category, title, detail, suggested_fix (nullable); `ReviewResult`: model_name, findings list or error). Define `ReviewState` (TypedDict with an `operator.add`-annotated results list) in `state.py`. Implement one review node factory in `nodes.py` using the shared system prompt and `with_structured_output(ReviewResult)`, wired to a minimal two-node graph (context → reviewer). **Verify current model IDs against each provider's official docs before setting the config defaults.**

**Acceptance criteria:**
- [ ] One model reviews a real PR diff and returns schema-valid findings referencing real files from the diff
- [ ] A clean/trivial diff yields an empty findings list, not invented findings
- [ ] The reviewer node catches exceptions and timeouts (config value) and records them as an error in state instead of raising

**Verification:**
- [ ] Unit test: node returns a recorded error (not an exception) when the LLM call raises: `pytest tests/test_nodes.py`
- [ ] Manual check: run against a real PR with one provider key; inspect findings JSON

**Dependencies:** Task 3
**Files likely touched:** `src/tri_review/schema.py`, `src/tri_review/state.py`, `src/tri_review/nodes.py`, `src/tri_review/config.py`, `tests/test_nodes.py`
**Estimated scope:** Medium

---

## Task 5: Three-model fan-out with failure tolerance

**Description:** Generalize the reviewer node factory across the three providers and assemble the full LangGraph in `graph.py`: context → {model_a, model_b, model_c} → synthesizer-placeholder → END, with the fan-out running as a parallel superstep. Confirm one provider failing does not abort the others.

**Acceptance criteria:**
- [ ] All three models run concurrently (wall time ≈ slowest model, not the sum)
- [ ] With one provider key removed, the other two results still land in state with the failure recorded (PRD acceptance criterion 3)
- [ ] Each result in state is attributed to its model name

**Verification:**
- [ ] Unit test with stubbed LLMs: three results aggregate via `operator.add`; one stub raising still yields two results plus one recorded error: `pytest tests/test_graph.py`
- [ ] Manual check: real run with all three keys, then with one key removed

**Dependencies:** Task 4
**Files likely touched:** `src/tri_review/nodes.py`, `src/tri_review/graph.py`, `tests/test_graph.py`
**Estimated scope:** Small

---

## Task 6: Synthesizer and report

**Description:** Implement the synthesizer node: guard on ≥2 successful reviews (otherwise write a structured failure into state for the CLI to exit non-zero with the collected errors); prompt a strong model with the structured findings lists to match same-issue findings across models and produce the three-section Markdown report (Consensus Findings, Unique Insights with model attribution, Actionable Next Steps with file/line references), noting any failed model.

**Acceptance criteria:**
- [ ] Findings flagged by 2+ models appear once under Consensus; single-model findings appear under Unique Insights with attribution
- [ ] With exactly 2 successful reviews, synthesis proceeds and the report notes the failed model
- [ ] With ≤1 successful review, the run exits non-zero with the provider errors (PRD acceptance criterion 4)

**Verification:**
- [ ] Unit tests for the ≥2 guard and report assembly with canned findings: `pytest tests/test_synthesizer.py`
- [ ] Manual check: real 3-model run on a PR with a planted bug; the bug lands in Consensus

**Dependencies:** Task 5
**Files likely touched:** `src/tri_review/nodes.py`, `src/tri_review/graph.py`, `tests/test_synthesizer.py`
**Estimated scope:** Medium

---

### Checkpoint: Core pipeline
- [ ] End-to-end: `tri-review --pr <n>` on a real PR produces a correct three-section report (PRD acceptance criteria 1–4)
- [ ] Review with human before polish

### Phase 3: CLI polish

---

## Task 7: Rich terminal UX and output flag

**Description:** Replace plain prints with Rich: spinner during context gathering, per-model status line (✓ done / ✗ failed with reason) during the fan-out, rendered Markdown report at the end, and a `--output <file>` flag writing the raw Markdown. Clean exit codes throughout (0 success; distinct non-zero for preflight vs. insufficient-reviews failures).

**Acceptance criteria:**
- [ ] Progress is visible during the run; per-model success/failure shown as results land
- [ ] Final report renders as formatted Markdown in the terminal; `--output` writes identical raw Markdown to disk
- [ ] Exit codes: 0 success, non-zero for each failure class

**Verification:**
- [ ] Manual check: full run, run with a dead key, run with `--output report.md`; `echo $?` after each

**Dependencies:** Task 6
**Files likely touched:** `src/tri_review/cli.py`, `src/tri_review/graph.py`
**Estimated scope:** Small

---

## Task 8: README and acceptance pass

**Description:** Rewrite `README.md` with install (`pipx install` / `pip install -e .`), `gh` prerequisite, `.env` setup, usage examples, and a sample report. Then run the full PRD acceptance checklist (criteria 1–6) against a real repository and fix anything that fails.

**Acceptance criteria:**
- [ ] A new user can go from clone to first review using only the README
- [ ] All six PRD acceptance criteria pass, demonstrated against a real PR

**Verification:**
- [ ] Execute each PRD acceptance criterion and record the result in the PR description
- [ ] `pytest` fully green

**Dependencies:** Task 7
**Files likely touched:** `README.md`, misc fixes
**Estimated scope:** Small

---

### Checkpoint: Complete
- [ ] All PRD acceptance criteria met; tests green; ready for review

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Model IDs from memory/old docs are deprecated at runtime | High | Task 4 explicitly verifies current IDs against provider docs; IDs are env-overridable so a dead default never requires a code change |
| `with_structured_output` behaves differently across the three providers | Medium | Task 4 proves the schema on one provider first; Task 5 extends provider-by-provider instead of all at once |
| LangGraph parallel superstep aborts all branches when one node raises | Medium | Nodes never raise by design (record-error pattern); Task 5 has an explicit test for this |
| Synthesizer consensus matching is unreliable | Medium | Structured findings (file/line/category) make matching mostly mechanical; Task 6 manual check plants a known bug and confirms it reaches Consensus |
| Huge PRs blow the context budget | Low (MVP) | Truncation policy with visible warning (Task 3); chunking deferred per PRD |

## Open Questions

- None blocking. Post-MVP direction (`--comment`, chunking, streaming) is listed in the PRD scope table and intentionally excluded here.

# Product Requirements Document (PRD): `tri-review` CLI

## 1. Product Overview

**Name:** `tri-review` (Triangulated PR Reviewer)

**Goal:** A CLI application executed at the root of a local git repository. It fetches an open Pull Request, gathers local repository context, processes the PR through three distinct LLMs concurrently, and outputs a synthesized comparison highlighting the highest-value code changes to make.

**Target Audience:** Software engineers, tech leads, and open-source maintainers who want rigorous AI code reviews where cross-model consensus filters out single-model hallucinations.

**Core value proposition:** One model hallucinates confidently; three models rarely hallucinate the *same* thing. Findings flagged by 2+ independent models are high-trust; findings from one model are surfaced but labeled as unverified.

## 2. MVP Scope

| In scope (MVP) | Out of scope (post-MVP) |
|---|---|
| Fetch real PR diff via `gh` CLI | GitHub API / PyGithub integration |
| Auto-detect PR from current branch | GitLab / Bitbucket support |
| Full-file context for changed files | Dependency-graph context mapping |
| Truncation with warning when over token budget | Smart chunking / multi-pass review of huge diffs |
| 3-model concurrent review + synthesis | Configurable number of models |
| Rich terminal Markdown report | `--comment` flag to post review to the PR |
| Structured JSON findings per model | Streaming per-node progress output |

## 3. User Flow

1. The user navigates to their local repository in the terminal.
2. The user runs `tri-review --pr 123`, or simply `tri-review` to auto-detect the open PR for the current branch.
3. The CLI fetches the PR diff and reads the full local contents of every file the diff touches.
4. The CLI concurrently sends the context and diff to Model A, Model B, and Model C, each returning **structured findings** (not free text).
5. A Synthesizer module cross-references the three findings lists, identifying consensus and labeling single-model findings.
6. The CLI renders a formatted Markdown report in the terminal:
   - **Consensus Findings:** Issues flagged by 2 or more models (grouped, deduplicated).
   - **Unique Insights:** High-value observations from a single model, labeled as such.
   - **Actionable Next Steps:** Specific code changes, with file/line references and suggested snippets.

## 4. Core Features & Requirements

### 4.1. PR Retrieval & Context Engine

- **Mechanism (decided):** Shell out to the GitHub CLI (`gh`). `gh pr diff <n>` fetches the diff; `gh pr view --json number` auto-detects the current branch's PR. This eliminates GITHUB_TOKEN management (auth is `gh auth login`, which users already have) and gets branch detection for free.
- **Preflight checks:** On startup, verify `gh` is installed, authenticated, and the cwd is a git repo with a GitHub remote. Fail fast with an actionable message for each case.
- **Repo context (decided):** For each file modified in the diff, include its full current content from the local checkout. No dependency mapping in MVP. Deleted files contribute only their diff hunk.
- **Token management (decided):** A configurable total context budget (default ~100k tokens, estimated at 4 chars/token). If diff + file contents exceed the budget, drop full-file contents largest-first (keeping their diff hunks) until under budget, and print a visible warning listing what was truncated. No chunking in MVP.

### 4.2. Multi-Model Orchestration (The Graph)

- **Concurrent execution:** The three review calls run in parallel (LangGraph fan-out).
- **Structured output (decided):** Each model must return findings as structured JSON conforming to a shared schema — `file`, `line` (nullable), `severity` (critical/major/minor), `category` (bug/security/performance/logic), `title`, `detail`, `suggested_fix` (nullable). Free-text reviews are not acceptable; structure is what makes cross-model matching reliable.
- **Agnostic prompt:** One standardized system prompt for all three models: behave as an expert reviewer, focus on bugs, security, logic, and performance; explicitly ignore style nitpicks; return an empty findings list if the diff is clean (no invented findings).
- **Model configuration:** Model IDs live in config with environment-variable overrides (`TRI_REVIEW_MODEL_A`, etc.). Defaults are the current flagship of each provider (OpenAI, Anthropic, Google) and must be verified against provider docs at implementation time — never hardcoded deep in node code.
- **Failure tolerance (decided):** If a model call fails or times out (default 120s), its node records the failure and the run continues. Synthesis proceeds if **≥2 models** returned findings; the report notes which model failed and why. If ≤1 model succeeds, the run exits non-zero with the errors — a single-model review defeats the product's purpose.

### 4.3. Synthesis & Comparison Engine

- **Consensus algorithm (decided):** A final LLM call (the Synthesizer) receives the structured findings lists from all successful models. Because inputs are structured (file/line/category), it matches findings that describe the same underlying issue even when worded differently, then produces the three-section report. Findings matched across 2+ models → Consensus; others → Unique Insights, attributed to their model.
- **Output:** Markdown rendered to the terminal. `--output <file>` optionally writes the raw Markdown to disk.

### 4.4. Output & UX

- **Terminal UI:** `Rich` for spinners during parallel review, per-model status (✓ / ✗ with error), and final Markdown rendering.
- **Exit codes:** 0 on success, non-zero on preflight failure or <2 successful reviews (script/CI friendly).

## 5. Architecture: The Graph Workflow

A LangGraph state machine:

1. **State:** `{pr_number, diff, context, findings: [per-model], errors: [per-model], final_report}`
2. **Node 1 (Context Builder):** Runs `gh` preflight, fetches diff, reads changed files, applies token budget. Updates state.
3. **Parallel Nodes (Fan-Out):** Node 2A/2B/2C each prompt one model with the shared prompt + schema, appending structured findings (or a recorded error) to state.
4. **Node 3 (Synthesizer / Fan-In):** Guards on ≥2 successful reviews, cross-references findings, writes `final_report`.
5. **Node 4 (Output):** Renders `final_report` via Rich.

## 6. Tech Stack (decided)

- **Language:** Python 3.11+
- **Orchestration:** LangGraph (Python) with LangChain provider packages (`langchain-openai`, `langchain-anthropic`, `langchain-google-genai`) and `with_structured_output` for the findings schema
- **CLI:** `Click`; **Terminal UI:** `Rich`
- **Git/GitHub:** `gh` CLI + native `git`, via subprocess — no GitHub API library
- **Packaging:** `pyproject.toml` with a `tri-review` console entry point
- **Config:** `.env` via `python-dotenv` for the three provider API keys; model IDs and token budget overridable via env vars

## 7. Acceptance Criteria (MVP is done when)

1. From the root of a real repository with an open GitHub PR, `tri-review --pr <n>` produces a synthesized three-section Markdown report in the terminal in under ~3 minutes, with findings referencing real files/lines from the PR.
2. `tri-review` with no arguments auto-detects and reviews the current branch's open PR.
3. With one provider key removed, the run completes on the remaining two models and the report notes the failure.
4. With only one working provider, the run exits non-zero with a clear message.
5. Running against a diff exceeding the token budget prints a truncation warning naming the dropped files and still completes.
6. Missing `gh`, unauthenticated `gh`, or a non-repo cwd each produce a specific, actionable error — not a stack trace.

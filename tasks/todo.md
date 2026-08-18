# tri-review — Task Checklist

Active plan: `plan.md` (full-stack app). Shipped MVP details: `../implementation-plan.md`.

---

# ACTIVE: Full-stack app

Turn the CLI into a local-first web app: pick any repo and PR, click Review, watch
progress live, keep a browsable history. Manual trigger, single user, no login.
Full task detail in `plan.md`.

**Scope change from the plan, on request:** the goal is *paste a PR URL, read
three reviews side by side in Markdown*. So the repo browser became a URL paste
box (no `/api/repos` endpoints, no `list_repos`), per-model Markdown became the
primary view rather than the synthesized report, and the BYOK settings page was
dropped. Phases 1–2 were built as planned.

## Phase 1: Make the core repo-addressable (CLI stays green)
- [x] Task 1: Repo-parameterized GitHub client (`--repo owner/name`, `--url`, PR head SHA, fetch file contents, `parse_pr_url`)
- [x] Task 2: Pluggable context reader (filesystem + GitHub), threaded through `graph.py`/`state.py`, path guard at the boundary
- [x] Task 2b: Extract `preview_context()` + a `ContextSummary` Pydantic model, plus `render_findings_md()` for the side-by-side view

## Checkpoint A
- [x] All existing tests green (72 -> 138); CLI works both cwd-style and `--repo`/`--url`-style
- [x] A `--repo` review from an unrelated directory reads file contents at the PR's head SHA —
      proven live on octocat/Hello-World#10856: README at head `8dc060f` carries the PR's added
      line, master does not, and the built context contains the head version

## Phase 2: Backend
- [x] Task 3: FastAPI skeleton + `/api/health` (reports provider-key presence) + `/api/pulls/resolve`
- [x] Task 4: Persistence + review jobs (SQLite, `POST /api/reviews`, `/preview` with no model calls)
- [x] Task 5: Live progress over SSE (`GET /api/reviews/{id}/events`)

## Checkpoint B
- [x] A full review is drivable end-to-end with `curl` alone — no frontend needed
- [x] Streaming confirmed unbuffered over real HTTP: models staggered 1.0/2.5/4.0s
      arrived at 3.64/5.13/6.64s, ~1.5s apart, not batched

## Phase 3: Frontend
- [x] Task 6: Next.js app + URL paste box (replaces the PR picker)
- [x] Task 7: Review page — three-column side-by-side Markdown, live via SSE, preview-before-run confirm
- [x] Task 8: History list and error surfaces (settings/BYOK page dropped as out of the stated goal)

## Checkpoint C
- [x] End-to-end from the browser against the real PR octocat/Hello-World#10856:
      preview -> run -> three columns filling in -> consensus -> history
- [x] A fresh load of a finished review replays identically from the database
- [x] A panel with one model failing keeps its column, shows the error, and the other two still report
- [x] CLI still green (157 tests)

---

# SHIPPED: CLI MVP

All tasks complete. Merged via PR #1.

## Phase 1: Foundation (real data, no LLMs)
- [x] Task 1: Project scaffolding and config
- [x] Task 2: PR fetching via `gh` (preflight + diff + auto-detect)
- [x] Task 3: Context builder with token budget (`--dry-run`)

## Checkpoint: Foundation
- [x] `pytest` green; `tri-review --pr <n> --dry-run` works on a real PR with no API keys

## Phase 2: Review pipeline
- [x] Task 4: Findings schema, graph state, one working reviewer (verify current model IDs against provider docs)
- [x] Task 5: Three-model fan-out with failure tolerance
- [x] Task 6: Synthesizer and report (≥2-model guard)

## Checkpoint: Core pipeline
- [x] End-to-end run on a real PR produces a correct three-section report
- [x] Review with human before polish — user authorized running to completion up front

## Phase 3: CLI polish
- [x] Task 7: Rich terminal UX, `--output` flag, exit codes
- [x] Task 8: README and full PRD acceptance pass (criteria 1–6)

## Checkpoint: Complete
- [x] All PRD acceptance criteria met; tests green; ready for review

## Follow-on (post-plan, same branch)
- [x] Repeatable `--model` flag to select the review panel per run

## PRD acceptance results

Verified against the real public PR octocat/Hello-World#10856, plus a synthetic
diff carrying three planted defects (MD5 password hash, off-by-one, SQL injection).

| # | Criterion | Result |
|---|---|---|
| 1 | Real PR produces a three-section report in under ~3 min | PASS — 3 models, ~17s wall time; all three planted defects reached Consensus |
| 2 | No-argument invocation auto-detects the branch's PR | PASS — detected #10856 after `gh pr checkout` |
| 3 | One provider key removed: run completes on the rest, failure noted | PASS — exit 0, failure banner names the keyless model |
| 4 | Only one working provider: exit non-zero with the errors | PASS — exit 4, both provider errors reported |
| 5 | Over-budget diff: truncation warning, run still completes | PASS — warning names the dropped file, exit 0 |
| 6 | Missing gh / unauthenticated / non-repo give actionable errors | PASS for missing gh (exit 2), non-repo (exit 2), unknown PR (exit 3). Unauthenticated gh is unit-tested only — verifying live would mean logging out of the user's gh session. |

Not verifiable in this environment: the Anthropic and Google adapter paths, since
only `OPENAI_API_KEY` was available. Multi-model behaviour was exercised with three
different OpenAI models; the two other adapters are covered by unit tests and were
observed failing cleanly (recorded as errors, run continued) when their keys were absent.

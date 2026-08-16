# tri-review — Task Checklist

Active plan: `plan.md` (full-stack app). Shipped MVP details: `../implementation-plan.md`.

---

# ACTIVE: Full-stack app

Turn the CLI into a local-first web app: pick any repo and PR, click Review, watch
progress live, keep a browsable history. Manual trigger, single user, no login.
Full task detail in `plan.md`.

## Phase 1: Make the core repo-addressable (CLI stays green)
- [ ] Task 1: Repo-parameterized GitHub client (`--repo owner/name`, list repos/PRs, fetch file contents)
- [ ] Task 2: Pluggable context reader (filesystem reader + GitHub reader)

## Checkpoint A
- [ ] All 56 existing tests green; CLI works both cwd-style and `--repo`-style

## Phase 2: Backend
- [ ] Task 3: FastAPI skeleton + browse endpoints (`/api/health`, `/api/repos`, `/api/repos/{owner}/{repo}/pulls`)
- [ ] Task 4: Persistence + review jobs (SQLite, `POST /api/reviews`, `/preview` with no model calls)
- [ ] Task 5: Live progress over SSE (`GET /api/reviews/{id}/events`)

## Checkpoint B
- [ ] A full review is drivable end-to-end with `curl` alone — no frontend needed
- [ ] Highest-risk phase: confirm streaming and persistence before any UI work

## Phase 3: Frontend
- [ ] Task 6: Next.js app + PR picker (types generated from the OpenAPI schema)
- [ ] Task 7: Review page with live progress + preview-before-run confirm step
- [ ] Task 8: History, error surfaces, BYOK-ready settings page

## Checkpoint C
- [ ] End-to-end from the browser against a real PR; history survives restart; CLI still green

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

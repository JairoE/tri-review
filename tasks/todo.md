# tri-review MVP — Task Checklist

Full task details (acceptance criteria, verification, files): `../implementation-plan.md`

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

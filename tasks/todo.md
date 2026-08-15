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
- [ ] Task 6: Synthesizer and report (≥2-model guard)

## Checkpoint: Core pipeline
- [ ] End-to-end run on a real PR produces a correct three-section report
- [ ] Review with human before polish

## Phase 3: CLI polish
- [ ] Task 7: Rich terminal UX, `--output` flag, exit codes
- [ ] Task 8: README and full PRD acceptance pass (criteria 1–6)

## Checkpoint: Complete
- [ ] All PRD acceptance criteria met; tests green; ready for review

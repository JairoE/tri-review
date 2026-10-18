# tri-review Product PRD

The durable product definition. Feature work is specified under
`docs/features/<slug>/`; this file holds only what every feature must respect.
The original MVP spec is kept for history at
[`docs/archive/mvp/PRD.md`](../archive/mvp/PRD.md).

## Vision

One model hallucinates confidently; independent models rarely hallucinate the
*same* thing. tri-review sends a pull request to a panel of LLMs from different
providers, each reviewing blind, and reports which findings two or more of them
agree on. Single-model findings are surfaced but labelled unverified.

It ships as a CLI (`tri-review`), a GitHub Action (`action.yml`), and a Claude
Code skill (`.claude/skills/tri-review/`).

## Users

Engineers, tech leads and maintainers who want a rigorous AI review where
agreement across providers, not one model's confidence, decides what is worth
acting on.

## Global constraints

Every feature must hold these. A feature that needs to break one needs an ADR
under `docs/adr/`.

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
7. **Spend is visible and avoidable.** Gates (paths, triage, cap, replay,
   cache) run before any paid call; `--dry-run` calls nothing.
8. **The Action's pass/fail is the review's result.** It is decided in the
   "Enforce result" step from the CLI's exit code, after the report is posted.

## Feature specs

| Feature | Status | Spec |
|---|---|---|
| Actionable findings: Problem/Trigger/Fix, exact lines, inline suggested changes | Specified | [`docs/features/actionable-findings/PRD.md`](../features/actionable-findings/PRD.md) |

## Decisions

| ADR | Decision |
|---|---|
| [0001](../adr/0001-structured-synthesis.md) | The synthesizer returns structured findings; tri-review renders the report |

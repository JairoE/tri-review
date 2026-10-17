# tri-review Product PRD

## Vision

Several independent LLMs review a GitHub pull request, and a synthesizer reports which findings two or more of them agree on. One model can be confidently wrong, but independent models rarely make the *same* mistake. Findings that models agree on are high-trust. Findings from a single model are still shown, labelled unverified.

tri-review ships as a CLI (`tri-review`), a GitHub Action (`JairoE/tri-review@v1`), and a Claude Code skill.

## Global Constraints

These apply to every feature. A feature spec may add constraints but must not relax these.

1. **Never hide a finding.** A finding may be downgraded, labelled unverified, or marked previously dismissed, but it is never silently dropped from the report.
2. **Reviewers stay independent.** Reviewers never see each other's output, the dismissal ledger, or the synthesizer's output.
3. **No PR-authored code runs with secrets.** Nothing tri-review does executes code from the PR under review. Paths, labels, and comment text written by the PR author or commenters are data, never shell.
4. **Spend is visible and avoidable.** Gates (excludes, replay, cache, triage, cap) run before any model call. `--dry-run` never calls a model.
5. **Failures degrade, they don't vanish.** A failed reviewer, synthesizer, or GitHub write is reported in the output. Model calls that already succeeded are never thrown away.
6. **`gh` is the only GitHub client.** All GitHub access goes through the `gh` CLI (`gh api` included). There is no GitHub SDK dependency.
7. **The PR head SHA is the source of truth.** File contents, line numbers, and stored reports are tied to the head SHA the review read.

## Feature Specs

| Feature | Status | Spec |
|---|---|---|
| MVP: three-model CLI review | Shipped (archived spec) | [docs/archive/mvp/PRD.md](../archive/mvp/PRD.md) |
| Actionable findings: sharper findings, code highlights, apply on GitHub | Planned | [docs/features/actionable-findings/PRD.md](../features/actionable-findings/PRD.md) |

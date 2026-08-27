---
name: tri-review
description: >
  Run the tri-review CLI to have three LLMs (OpenAI/Anthropic/Google) independently
  review a GitHub pull request and report which findings 2+ models agree on. Use when
  the user says "review this PR with tri-review", "triangulated review", "run
  tri-review", or asks for a second opinion on a PR from multiple independent models.
---

# tri-review: triangulated PR review

A thin wrapper around the `tri-review` CLI. It already does the review and the
synthesis — this skill's job is to run it correctly and relay what it says, not
to re-review the PR or reformat its output.

## Step 1 — preflight

Check the CLI is installed: `which tri-review`. If it isn't, tell the user and
point at the install command in this repo's README (`uv sync` or
`pip install -e .`) rather than installing it yourself unprompted.

## Step 2 — determine the target

- If the user named a PR number, URL, or repo, pass it through: `--pr N`,
  `--url <pasted URL>`, or `--repo owner/name --pr N`.
- Otherwise, just run `tri-review` bare — it autodetects the open PR for the
  current branch. This only works from inside a checkout of the repo the PR
  belongs to, which is the normal case for a session already working on that PR.

## Step 3 — run it

```bash
tri-review --pr <N>
```

If the user seems unsure about cost or scope before committing to a real run,
mention `--dry-run` — it shows exactly what would be sent (files, estimated
tokens) without calling any model or needing API keys.

## Step 4 — handle known failures with a specific next step, not a raw traceback

tri-review's exit code says exactly what went wrong:

| Exit | Meaning | What to tell the user |
|---|---|---|
| `2` | Preflight failed — no `gh`, not authenticated, not a repo, or a bad flag | If it's `gh`: install from cli.github.com. If unauthenticated: run `gh auth login` yourself, don't attempt an interactive login on the user's behalf. |
| `3` | No such PR | Ask for a PR number or URL, or confirm one is actually open on the current branch. |
| `4` | Fewer than two models reported | Usually missing/invalid API keys. At least two of `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY` must be set (env or `.env` in the run directory) — point at whichever the error output names as missing. |

## Step 5 — present the report

tri-review prints a Markdown report (Consensus Findings / Unique Insights /
Actionable Next Steps). Relay it back to the user as-is — don't summarize it
into something shorter or rewrite its structure. The whole point of the tool is
that the synthesis already happened.

## Boundaries

- Don't modify code based on the findings unless the user separately asks you to.
- Don't manage API keys or run `gh auth login` on the user's behalf.
- Don't retry a failed run automatically — surface the exit code and let the
  user decide (e.g., a rate-limited provider might just need a minute).

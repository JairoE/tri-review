# tri-review

Three LLMs (OpenAI, Anthropic, Google) review your pull request independently. A synthesizer then reports:

- **Consensus Findings**: flagged by 2 or more models.
- **Unique Insights**: flagged by one model, marked unverified.
- **Actionable Next Steps**: the changes worth making, with file and line references.

## Quickstart

### GitHub Action (recommended)

Reviews every PR in CI and posts the report as a PR comment. Nothing to install locally.

1. Add `.github/workflows/tri-review.yml` to your repo:

   ```yaml
   name: tri-review
   on:
     pull_request:
       types: [opened, synchronize, reopened]

   permissions:
     contents: read
     pull-requests: write
     issues: write   # lets the Action mark old reports "outdated"

   jobs:
     review:
       runs-on: ubuntu-latest
       steps:
         - uses: actions/checkout@v4
           with:
             ref: ${{ github.event.pull_request.head.sha }}   # required: the PR head, not the merge commit
         - uses: JairoE/tri-review@v1
           with:
             openai-api-key: ${{ secrets.OPENAI_API_KEY }}
             anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
             google-api-key: ${{ secrets.GOOGLE_API_KEY }}
   ```

2. Add at least two of `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY` as secrets under **Settings → Secrets and variables → Actions**.
3. Open a PR.

All inputs are listed under [GitHub Action](#github-action).

### CLI

Requires Python 3.11+ and the [GitHub CLI](https://cli.github.com) (`gh`), logged in with `gh auth login`.

```bash
git clone https://github.com/JairoE/tri-review.git
cd tri-review
uv tool install .    # puts `tri-review` on PATH
```

Export at least two provider keys (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`), or put them in a `.env` in the directory you run from (see `.env.example`). Then review any PR `gh` can see, from any directory:

```bash
tri-review --url https://github.com/your-org/your-repo/pull/123
tri-review --repo your-org/your-repo --pr 123
```

### Claude Code skill

1. Install the CLI as shown above.
2. Copy `.claude/skills/tri-review/` from this repo to `~/.claude/skills/tri-review/`.
3. In a checkout with the PR's branch checked out, ask Claude Code to "review this PR with tri-review". It runs the CLI and returns the report verbatim.

## CLI usage

### Pointing at a PR

| Command | File contents come from |
|---|---|
| `tri-review --url <PR URL>` | GitHub, at the PR's head commit |
| `tri-review --repo owner/name --pr 123` | GitHub, at the PR's head commit |
| `tri-review --pr 123` | Your working tree |
| `tri-review` | Your working tree (reviews the current branch's open PR) |

When you run without `--repo` or `--url`, do it from the repo root **with the PR's branch checked out** (`gh pr checkout 123`). If a different branch is checked out, the models see the right diff next to the wrong file contents.

### Flags

| Flag | Effect |
|---|---|
| `--pr N` | PR number. Omit to use the current branch's PR |
| `--repo owner/name` | Review a PR without a checkout |
| `--url URL` | Sets `--repo` and `--pr` from a PR URL |
| `--dry-run` | Show the resolved panel and what would be sent. No model calls, no keys needed |
| `--output FILE` | Also save the Markdown report to a file |
| `--reviewer SPEC` | Add a reviewer (repeatable). See [Choosing models](#choosing-models) |
| `--synthesizer SPEC` | Model that writes the report |
| `--effort LEVEL` | Effort for reviewers that don't set their own `@effort` |
| `--exclude GLOB` | Leave matching files out (repeatable, on top of the defaults) |
| `--no-default-excludes` | Turn off the built-in exclude set |
| `--triage` / `--no-triage` | Turn the behaviour-change check on or off (see below) |
| `--fresh` | Skip the stored report and the reviewer cache and run a new review |

### Skipping and caching

These checks run before any model is called:

- **Docs and generated files are excluded.** Markdown, `docs/` prose, lockfiles, snapshots and images are dropped from the payload. If a PR changes only files like that, the run prints `Nothing to review` and exits `0`. Lockfile-only PRs are the exception: they still get reviewed. Use `--no-default-excludes` to review prose, or set `TRI_REVIEW_EXCLUDE` to replace the default set.
- **An unchanged PR replays its last report.** Reports are stored under `~/.cache/tri-review/`. If the head SHA, the model panel and the exclude patterns all match a stored run, you get that report back without any model calls. When running from a checkout, `HEAD` must also be the PR's head commit, with no uncommitted changes. Use `--fresh` to force a new review.
- **Only failed reviewers are retried.** Each reviewer call is cached by the exact content it was sent. If one provider fails, re-running calls only that one. Cache entries expire after 14 days (`TRI_REVIEW_CACHE_TTL_DAYS`). You can delete the cache directory at any time.
- **Triage (optional, off by default).** `--triage` asks the cheapest configured model whether the diff changes behaviour at all, and skips the review if it doesn't. When unsure, it reviews. Enable it for every run with `TRI_REVIEW_TRIAGE=1` or `triage: true` in the Action. `--dry-run` never runs triage.

## Choosing models

Every run has **reviewers** (any number) and one **synthesizer**, which may be one of the reviewers. By default the reviewers are the three configured slots (see [Configuration](#configuration)), and the synthesizer is the first reviewer.

Name a model with a spec:

```
[provider:]model[@effort]
```

| Part | Required | Example | Meaning |
|---|---|---|---|
| `provider:` | Only when the ID doesn't imply it | `openai:` | `gpt-`, `o1`, `o3`, `o4`, `ft:` → OpenAI; `claude-` → Anthropic; `gemini-3` → Google |
| `model` | Yes | `gpt-6-sol` | The provider's model ID |
| `@effort` | No | `@high` | `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` |

The model IDs below are examples. Check them against your provider's model list.

```bash
# Mixed panel, synthesizer at medium effort, reviewers at high
tri-review --pr 123 \
  --synthesizer gpt-6-astra@medium \
  --reviewer gpt-6-sol@high \
  --reviewer claude-opus-5@high \
  --reviewer gemini-3.8-flash

# Same effort for every reviewer that doesn't set its own (--effort never applies to the synthesizer)
tri-review --pr 123 --effort high --reviewer gpt-6-sol --reviewer claude-sonnet-5
```

In the Action, `reviewers` takes the same specs separated by spaces or newlines:

```yaml
- uses: JairoE/tri-review@v1
  with:
    synthesizer: gpt-6-astra@medium
    reviewers: |
      gpt-6-sol@high
      claude-opus-5@high
    openai-api-key: ${{ secrets.OPENAI_API_KEY }}
    anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
```

Notes:

- You need a key only for the providers your panel and synthesizer use.
- Mix providers. A panel that uses only one provider still works, but its report opens with a banner saying its consensus is weak evidence.
- The same model at two effort levels counts as two reviewers. Duplicate specs are merged into one.
- A single reviewer is allowed, and the report says nothing was corroborated.
- A bare `gemini-` ID that isn't Gemini 3 (for example `gemini-flash-latest`) needs a `google:` prefix. Gemini runs at `high` effort unless the spec says otherwise, because lower levels often return empty reviews.
- Invalid specs are reported before anything is fetched.
- `--model` / `models` are older names for `--reviewer` / `reviewers`.

## Dismissing a finding

To stop a settled finding from coming back, add it to `.tri-review/dismissed.toml` in your repo:

```toml
[[dismissed]]
file = "src/app/client.py"        # optional
claim = "retry loop can spin forever when the server returns 429"
reason = "checked -- backoff is capped at 5 attempts in _retry(), see the test"
date = "2026-01-14"               # optional
```

- `claim` and `reason` are required.
- The file is read from the PR's **base** branch, so a dismissal applies once it has been merged, not from inside the PR that adds it.
- Dismissed findings are not hidden. The report labels them as previously dismissed and shows your reason. Matching is by meaning, not exact wording.
- A finding comes back if the diff contains new evidence your recorded reason doesn't cover.

## GitHub Action

The [Quickstart](#github-action-recommended) workflow is the minimal setup. This repo's own [`.github/workflows/tri-review.yml`](.github/workflows/tri-review.yml) is a working example.

Each run posts a new comment and marks the earlier ones as outdated. If the token can't hide comments (no `issues: write`), older reports are folded into a collapsed `<details>` block instead. Nothing is deleted. Set `comment-mode: update` to keep a single comment and edit it in place.

A PR with nothing to review gets a short "Nothing to review" comment, and the check passes.

### Inputs

| Input | Default | Purpose |
|---|---|---|
| `openai-api-key` / `anthropic-api-key` / `google-api-key` | none | One per provider your models use. The default panel needs at least two |
| `reviewers` | the three configured slots | Model specs, separated by spaces or newlines |
| `synthesizer` | the first reviewer | Model spec that writes the report |
| `effort` | none | Effort for reviewers without their own `@effort` |
| `exclude` | none | Newline-separated globs, added to the default exclude set |
| `triage` | `false` | Skip the review if a cheap model finds no behaviour change |
| `max-reviews` | no cap | Maximum reports per PR. See [Capping reviews](#capping-reviews-per-pr) |
| `force` | `false` | `'true'` ignores `max-reviews` for this run |
| `fail-on-insufficient-reviews` | `true` | Whether exit code `4` fails the check or only posts a warning |
| `post-comment` | `true` | Post a PR comment at all |
| `comment-mode` | `append` | `append`: a new comment per run. `update`: edit one comment in place |
| `pr-number` | autodetected | Which PR to review |
| `github-token` | `${{ github.token }}` | Used for `gh` and for posting comments |
| `models` | none | Older name for `reviewers` |

### Outputs

| Output | Value |
|---|---|
| `report-path` | Absolute path of the report under `$RUNNER_TEMP/tri-review/`. Empty if no report was produced |
| `exit-code` | The CLI's exit code |
| `skipped` | `'true'` if no review was produced |
| `skip-reason` | `path` (every file excluded), `empty-diff`, `triage` (a model found no behaviour change), or `cap` (`max-reviews` reached) |

### Choosing the panel per PR

Use a label to pick a heavier panel for one run. With the setup below, every push gets the default panel, and adding the `tri-review:deep` label triggers one run with the heavier panel:

```yaml
on:
  pull_request:
    types: [opened, synchronize, reopened, labeled]

jobs:
  review:
    if: github.event.action != 'labeled' || github.event.label.name == 'tri-review:deep'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.sha }}
      - uses: JairoE/tri-review@v1
        with:
          reviewers: ${{ github.event.label.name == 'tri-review:deep' && 'gpt-6-sol@high gpt-5.6-sol@high claude-opus-5@high' || '' }}
          synthesizer: ${{ github.event.label.name == 'tri-review:deep' && 'gpt-6-astra@medium' || '' }}
          force: ${{ github.event.action == 'labeled' }}
          openai-api-key: ${{ secrets.OPENAI_API_KEY }}
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
          google-api-key: ${{ secrets.GOOGLE_API_KEY }}
```

An empty input (`''`) means "use the default".

### Capping reviews per PR

`max-reviews` limits how many reports a PR can get. With the setup below, adding the `tri-review:again` label buys one more review and then removes the label:

```yaml
name: tri-review
on:
  pull_request:
    types: [opened, synchronize, reopened, labeled]

permissions:
  contents: read
  pull-requests: write
  issues: write

jobs:
  review:
    if: github.event.action != 'labeled' || github.event.label.name == 'tri-review:again'
    concurrency:
      group: tri-review-${{ github.event.pull_request.number }}
      cancel-in-progress: false
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.sha }}
      - uses: JairoE/tri-review@v1
        with:
          max-reviews: 2
          force: ${{ github.event.action == 'labeled' }}
          openai-api-key: ${{ secrets.OPENAI_API_KEY }}
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
          google-api-key: ${{ secrets.GOOGLE_API_KEY }}
      - if: always() && github.event.action == 'labeled'
        env:
          GH_TOKEN: ${{ github.token }}
        run: gh api -X DELETE "repos/${{ github.repository }}/issues/${{ github.event.pull_request.number }}/labels/tri-review:again" || true
```

- Only runs that produce a report count toward the cap. Skips and failures don't.
- Once the cap is reached, later runs post a short note saying the commit was not reviewed, and the check passes.
- Keep the job-level `concurrency` group with `cancel-in-progress: false`. Without the group, two quick pushes can both slip under the cap. With cancellation on, a cancelled review is still billed but doesn't count. During a burst of pushes, GitHub runs the current review and the latest push, and skips the ones in between.
- The cap only counts comments posted by the token's own login. Use `github.token` or a personal access token, not a GitHub App token.
- With `post-comment: false`, the cap never triggers.
- If the PR's comments can't be read, the run goes ahead with a warning.

## Configuration

Environment variables. Defaults are in [`src/tri_review/config.py`](src/tri_review/config.py).

| Variable | Default | Purpose |
|---|---|---|
| `TRI_REVIEW_MODEL_A` | `gpt-5.6-terra` | First reviewer |
| `TRI_REVIEW_MODEL_B` | `claude-sonnet-5` | Second reviewer |
| `TRI_REVIEW_MODEL_C` | `gemini-3.8-flash` | Third reviewer |
| `TRI_REVIEW_SYNTHESIZER` | the first reviewer | Model that writes the report |
| `TRI_REVIEW_EFFORT` | unset | Effort for reviewers without their own `@effort` |
| `TRI_REVIEW_TOKEN_BUDGET` | `100000` | Maximum estimated tokens for diff + file contents |
| `TRI_REVIEW_TIMEOUT` | `120` | Per-model timeout in seconds (Google defaults to 300 unless this is set) |
| `TRI_REVIEW_EXCLUDE` | built-in set | Comma- or newline-separated globs that replace the default exclude set |
| `TRI_REVIEW_HISTORY_DIR` | `~/.cache/tri-review` | Where stored reports live |
| `TRI_REVIEW_CACHE_TTL_DAYS` | `14` | How long cached reviewer calls last. `0` = never expire |
| `TRI_REVIEW_TRIAGE` | unset | `1` turns on triage for every run |
| `TRI_REVIEW_TRIAGE_MODEL` | model C | Model used for triage |

Model variables accept full specs. Command-line flags override the matching variables.

## Behaviour and exit codes

- If one provider fails, the other reviewers still produce a report, and the failure is noted at the top.
- If the payload exceeds the token budget, the contents of the largest files are dropped (their diff hunks are kept), and a warning names them.
- Diff paths that point outside the repository (`../`, or a symlink added by the PR) are refused.

| Code | Meaning |
|---|---|
| `0` | Success, including "Nothing to review" |
| `2` | Environment problem: no `gh`, not authenticated, not a repo, bad flag |
| `3` | PR not found |
| `4` | Too few reviews for the panel (a panel of 2+ needs 2 reviews back) |
| `130` | Interrupted |

## Sample output

A diff that adds an MD5 password hash, an off-by-one error, and a SQL injection, reviewed by three OpenAI models (hence the single-provider banner):

```markdown
> **All 3 reviewers are `openai` models.** Models from one provider share training
> data and failure modes, so agreement between them is much weaker evidence than
> cross-provider consensus. Read the sections below as one opinion stated
> repeatedly, not as independent corroboration.

## Consensus Findings

1. **Insecure password hashing using MD5**
   - **File:** `auth.py`, approx. lines 4–5
   - **Severity:** Critical (security)
   - **Issue:** `hash_password` uses `hashlib.md5`, which is fast, unsalted, and
     cryptographically broken, making leaked hashes easy to brute-force.
   - **Reported by:** `gpt-5.1`, `gpt-4.1`, `gpt-4o`

2. **Out-of-bounds access in `find_user` loop**
   - **File:** `auth.py`, approx. lines 7–8
   - **Severity:** Major (bug)
   - **Issue:** `range(len(users) + 1)` indexes one past the end, raising
     `IndexError` whenever the user is not found.
   - **Reported by:** `gpt-5.1`, `gpt-4.1`, `gpt-4o`

## Unique Insights

None. All models reported the same underlying issues.
```

## Development

```bash
uv sync --extra dev      # or: pip install -e ".[dev]"
uv run pytest            # offline; all provider calls are stubbed
```

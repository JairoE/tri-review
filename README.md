# tri-review

Have three different LLMs review your pull request independently, then report what they agree on.

One model hallucinates confidently. Three models rarely hallucinate the *same* thing. `tri-review` sends a PR to OpenAI, Anthropic, and Google in parallel, then has a synthesizer separate corroborated findings from single-model guesses:

- **Consensus Findings** — flagged by 2 or more models. High trust.
- **Unique Insights** — flagged by one model, labeled unverified.
- **Actionable Next Steps** — the changes worth making, with file and line references.

## Requirements

- Python 3.11+
- The [GitHub CLI](https://cli.github.com) (`gh`), authenticated once with `gh auth login`. There is no `GITHUB_TOKEN` to manage — `tri-review` uses your existing `gh` session.
- API keys for at least two of the three providers.

## Install

With [uv](https://docs.astral.sh/uv/), which installs the exact dependency set this
project was built and tested against:

```bash
git clone https://github.com/JairoE/tri-review.git
cd tri-review
uv sync
```

Or with pip:

```bash
git clone https://github.com/JairoE/tri-review.git
cd tri-review
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e .
```

`uv.lock` is committed, so `uv sync` reproduces the resolved graph exactly. The pip
path resolves fresh against the floors in `pyproject.toml`, which are permissive —
prefer `uv sync` if a provider adapter misbehaves, since a version skew in the
`langchain-*` packages is the likeliest cause.

Then create a `.env` in the directory you run from (see `.env.example`):

```
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_API_KEY=...
```

Keys are read from the environment too, so an exported key works just as well.

## Usage

There are two ways to point `tri-review` at a PR: from inside a checkout, or at a
repo and PR number (or URL) from anywhere. Both send the models the same two
things — the PR diff and the full contents of each changed file — the difference
is only where the file contents come from.

### From a checkout (cwd mode)

Run from the root of the repository whose PR you want reviewed, **with that PR's
branch checked out**:

```bash
gh pr checkout 123       # do this first -- see below
tri-review --pr 123      # review a specific PR
tri-review               # review the current branch's open PR
tri-review --pr 123 --dry-run          # show what would be sent, call nothing
tri-review --pr 123 --output review.md # also save the raw Markdown
```

The checkout matters. The PR diff always comes from GitHub, but in this mode the
full contents of each changed file are read from **your working tree as it
currently stands**. Review PR #123 while sitting on `main` and the models get the
right diff paired with the wrong file bodies — a mismatch that invites exactly the
confident-but-wrong findings this tool exists to filter out. `gh pr checkout <n>`
first, and the two agree.

### From anywhere, by repo or URL (no checkout)

```bash
tri-review --repo octocat/Hello-World --pr 123
tri-review --url https://github.com/octocat/Hello-World/pull/123
```

No checkout, no `cd`. File contents are fetched from GitHub itself rather than
read off disk, at the PR's head commit SHA — the same hazard the checkout rule
exists to prevent (right diff, wrong file bodies), solved by always reading at
the ref the diff was actually generated against instead of by requiring your
working tree to match it. `--url` is shorthand: it just sets `--repo` and `--pr`
from a pasted GitHub PR URL, so it works with either the plain URL or one with a
trailing `/files` or query string.

`--dry-run` needs no API keys, which makes it a cheap way to check what context a
review would actually see before paying for one — including whether the files it
lists look like the PR's versions. It works in both modes.

Provider keys are read from the process environment, or from a `.env` in the
directory you run from. In cwd mode that's whichever repo you're reviewing, not
this one; in repo/url mode it could be anywhere, including `/tmp`. Either way,
exporting the keys in your shell profile is usually less trouble than keeping a
`.env` in every directory you might run from.

### Choosing the panel

Repeat `--model` to pick which models review the PR, overriding the configured slots:

```bash
tri-review --pr 123 \
  --model gpt-5.1 \
  --model claude-opus-5 \
  --model gemini-2.5-pro
```

At least two *distinct* models are required, since one model can't corroborate
anything — repeated IDs are collapsed before that count. More than three is allowed.
Unrecognized model IDs and too-short panels are rejected before the PR is fetched,
so a typo costs you nothing.

**Mix providers.** If you only hold one provider's key you can still fill the panel
from it:

```bash
tri-review --pr 123 --model gpt-5.1 --model gpt-4.1 --model gpt-4o
```

but understand what you are buying. The premise of this tool is that *independent*
models rarely hallucinate the same thing; two checkpoints of one family share
training data and failure modes, so they agree on each other's mistakes. A
single-provider run still works and still reports, but the report opens with a
banner saying its consensus is weak evidence. Cross-provider is the real product.

## Configuration

Every value is an environment variable override; defaults are in `src/tri_review/config.py`.

| Variable | Default | Purpose |
|---|---|---|
| `TRI_REVIEW_MODEL_A` | `gpt-5.1` | First reviewer |
| `TRI_REVIEW_MODEL_B` | `claude-opus-5` | Second reviewer |
| `TRI_REVIEW_MODEL_C` | `gemini-2.5-pro` | Third reviewer |
| `TRI_REVIEW_SYNTHESIZER` | same as model A | Model that cross-references the reviews |
| `TRI_REVIEW_TOKEN_BUDGET` | `100000` | Max estimated tokens for diff + file context |
| `TRI_REVIEW_TIMEOUT` | `120` | Per-model timeout in seconds |

The provider is chosen from the model ID prefix (`gpt-`, `o1`, `o3`, `o4`, `claude-`,
`gemini`), so
you can point any slot at any supported provider — including three models from the
same provider if you only have one key, with the caveat described above.

`--model` takes precedence over `TRI_REVIEW_MODEL_A/B/C`: use the env vars for your
standing default panel, and the flag for a one-off.

## How it behaves

- **A model that fails does not sink the run.** If one provider is down, rate-limited, or missing a key, the other two still produce a report and the failure is noted at the top.
- **Fewer than two reviews is an error.** A single-model review is just a code review, so `tri-review` exits non-zero rather than pretending it triangulated anything.
- **Large PRs degrade rather than fail.** If the diff plus changed-file contents exceed the token budget, the largest files' contents are dropped (their diff hunks are kept) and the dropped files are named in a warning. If the diff *alone* busts the budget, that is called out too — no file contents can be included and the providers may reject the payload.
- **Consensus between same-family models is labeled as weak.** If every reviewer that reported came from one provider, the report opens with a banner saying so rather than presenting their agreement as corroboration.
- **The diff is untrusted input.** Paths in it are written by whoever opened the PR, so a diff header pointing outside the repository — via `../` or a symlink the PR adds — is refused and named in the run output instead of being read and shipped to the model providers.

Exit codes: `0` success, `2` environment problem (no `gh`, not authenticated, not a repo, or a bad flag), `3` no such PR, `4` fewer than two reviews, `130` interrupted.

## Sample output

Against a diff introducing an MD5 password hash, an off-by-one, and a SQL injection.
This run used three OpenAI models, so it opens with the single-provider banner —
a cross-provider panel would not print it:

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

## GitHub Action

Drop `tri-review` into CI so it reviews every PR automatically, no local install
required. This repo's own `.github/workflows/tri-review.yml` is a working example —
it reviews `tri-review`'s own PRs.

```yaml
name: tri-review
on:
  pull_request:
    types: [opened, synchronize, reopened]

permissions:
  pull-requests: write

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: JairoE/tri-review@v1
        with:
          openai-api-key: ${{ secrets.OPENAI_API_KEY }}
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
          google-api-key: ${{ secrets.GOOGLE_API_KEY }}
```

The runner's own checkout already has the PR branch, so this runs the CLI's normal
cwd mode — the same local-checkout path described above, no `--repo`/`--url`
needed. The report is posted as a PR comment and updated in place on every push
(matched by a hidden marker), rather than piling up a new comment each time.

| Input | Default | Purpose |
|---|---|---|
| `pr-number` | autodetected | Which PR to review; usually left unset |
| `models` | the three configured slots | Space-separated model IDs, same rules as `--model` |
| `exclude` | none | Newline-separated glob patterns, same as `--exclude` |
| `fail-on-insufficient-reviews` | `true` | Whether exit code `4` (fewer than two reviews) fails the check or just posts a warning |
| `post-comment` | `true` | Whether to post/update a PR comment |
| `github-token` | `${{ github.token }}` | Used for both `gh auth` and posting the comment |
| `openai-api-key` / `anthropic-api-key` / `google-api-key` | none | At least two required |

## Claude Code skill

`.claude/skills/tri-review/` ships a skill that runs the CLI from inside a Claude
Code session — say "review this PR with tri-review" while sitting in a checkout
with an open PR. It's a thin wrapper: it runs the same `tri-review` command a
person would, and relays the Markdown report back verbatim rather than
re-summarizing it. Copy the same file to `~/.claude/skills/tri-review/` to make it
available in every session on your machine, regardless of which repo you're in.

## Development

```bash
pip install -e ".[dev]"
pytest
```

The suite is offline — every provider call is stubbed, so it runs without API keys.

## Architecture

A LangGraph state machine: a context node resolves the PR and assembles the payload,
three reviewer nodes fan out concurrently (wall time is the slowest model, not the
sum), and a synthesizer fans back in. Reviewers return structured `Finding` objects
rather than prose, which is what lets the synthesizer match the same issue across
models by file and line instead of comparing wording.

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

```bash
git clone https://github.com/JairoE/tri-review.git
cd tri-review
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e .
```

Then create a `.env` in the directory you run from (see `.env.example`):

```
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
GOOGLE_API_KEY=...
```

Keys are read from the environment too, so an exported key works just as well.

## Usage

Run from the root of the repository whose PR you want reviewed:

```bash
tri-review --pr 123      # review a specific PR
tri-review               # review the current branch's open PR
tri-review --pr 123 --dry-run          # show what would be sent, call nothing
tri-review --pr 123 --output review.md # also save the raw Markdown
```

`--dry-run` needs no API keys, which makes it a cheap way to check what context a
review would actually see before paying for one.

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

The provider is chosen from the model ID prefix (`gpt-`/`o3`, `claude-`, `gemini`), so
you can point any slot at any supported provider — including three models from
the same provider if you only have one key.

## How it behaves

- **A model that fails does not sink the run.** If one provider is down, rate-limited, or missing a key, the other two still produce a report and the failure is noted at the top.
- **Fewer than two reviews is an error.** A single-model review is just a code review, so `tri-review` exits non-zero rather than pretending it triangulated anything.
- **Large PRs degrade rather than fail.** If the diff plus changed-file contents exceed the token budget, the largest files' contents are dropped (their diff hunks are kept) and the dropped files are named in a warning.

Exit codes: `0` success, `2` environment problem (no `gh`, not authenticated, not a repo), `3` no such PR, `4` fewer than two reviews.

## Sample output

Against a diff introducing an MD5 password hash, an off-by-one, and a SQL injection:

```markdown
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

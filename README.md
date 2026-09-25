# tri-review

Have three different LLMs review your pull request independently, then report what they agree on.

One model hallucinates confidently. Three models rarely hallucinate the *same* thing. `tri-review` sends a PR to OpenAI, Anthropic, and Google in parallel, then has a synthesizer separate corroborated findings from single-model guesses:

- **Consensus Findings** — flagged by 2 or more models. High trust.
- **Unique Insights** — flagged by one model, labeled unverified.
- **Actionable Next Steps** — the changes worth making, with file and line references.

## Quickstart: use tri-review in your own repo

You don't need to clone this repo to use `tri-review` on your project. Pick whichever of these fits — they're ordered from least to most setup.

### Option A: GitHub Action (easiest, zero local install)

Reviews every PR automatically. Nothing to install on your machine.

1. In your repo, add `.github/workflows/tri-review.yml`:

   ```yaml
   name: tri-review
   on:
     pull_request:
       types: [opened, synchronize, reopened]

   permissions:
     contents: read
     pull-requests: write
     # `pull-requests: write` is enough to post the report comment. Without
     # `issues: write` too, a superseded report is folded into a collapsed
     # <details> block instead of GitHub's native "marked as outdated" -- see
     # "Each run posts its own comment" under GitHub Action, below.
     issues: write

   jobs:
     review:
       runs-on: ubuntu-latest
       steps:
         # Pin to the PR's actual head commit -- the default pull_request checkout
         # is a synthetic merge commit, which would pair the diff with the wrong
         # file contents.
         - uses: actions/checkout@v4
           with:
             ref: ${{ github.event.pull_request.head.sha }}
         # @v1 is a mutable tag; pin to a release commit SHA instead if you want
         # the run to be immune to the tag being retargeted.
         - uses: JairoE/tri-review@v1
           with:
             openai-api-key: ${{ secrets.OPENAI_API_KEY }}
             anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
             google-api-key: ${{ secrets.GOOGLE_API_KEY }}
   ```

2. Add at least two of `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY` as
   repository (or organization) secrets under **Settings → Secrets and variables →
   Actions**.
3. Open a pull request. `tri-review` runs automatically and posts its report as a
   PR comment. Every push gets its own comment, and the previous one is marked
   outdated and collapsed, so the PR keeps the whole review history in order.

No `gh` login, no Python install, no per-developer setup — the workflow does all
of it inside CI. See [GitHub Action](#github-action) below for the full list of
inputs (choosing models, excluding files, etc.).

### Option B: CLI, no checkout (one-off reviews)

Good for trying a single PR out before wiring up CI, or for reviewing PRs in repos
you don't want to check out locally.

1. Install once, anywhere on your machine, as a global command:

   ```bash
   git clone https://github.com/JairoE/tri-review.git
   cd tri-review
   uv tool install .   # puts `tri-review` on PATH; or activate a venv and `pip install -e .`
   ```
2. Authenticate `gh` if you haven't already: `gh auth login`.
3. Export at least two provider API keys in your shell (`OPENAI_API_KEY`,
   `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`).
4. From anywhere — no `cd` into the target repo required — run:

   ```bash
   tri-review --repo your-org/your-repo --pr 123
   # or paste the PR URL directly:
   tri-review --url https://github.com/your-org/your-repo/pull/123
   ```

This fetches the diff and file contents straight from GitHub at the PR's head
commit, so it works against any repo `gh` can see — public or private, as long as
you're authenticated.

### Option C: Claude Code skill (review from inside a Claude Code session)

Lets you say "review this PR with tri-review" while working in Claude Code.

1. Install `tri-review` per Option B, steps 1–3 (the skill just runs the CLI).
2. Copy `.claude/skills/tri-review/` from this repo to `~/.claude/skills/tri-review/`
   so it's available in every project, not just this one.
3. In any repo, with the PR's branch checked out, ask Claude Code to review it —
   e.g. "review this PR with tri-review." Claude runs the CLI and relays the
   Markdown report back verbatim.

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

### What gets skipped

A review costs three model calls against a payload that often runs past 100k
tokens, so `tri-review` decides whether a PR is worth that **before** it spends
anything. Two gates run first, both from the changed-file list alone — no diff
body, no file contents, no provider calls:

**Documentation and generated files are excluded by default.** Markdown, prose
under `docs/`, lockfiles, snapshots and images hold no program logic worth
triangulating. Every pattern that can skip a PR outright is anchored to a file
extension or an exact filename, never to a directory: `docs/conf.py` and
`docs/scripts/deploy.sh` are code, and are reviewed like any other code. On a
mixed PR they are simply dropped from the payload, so the token budget goes to
the code. On a PR that changes *nothing else*, the run stops:

```
$ tri-review --pr 51
Nothing to review. All 3 file(s) changed by PR #51 match the exclude patterns,
so there is no code to triangulate:
  - README.md
  - docs/setup.md
  - CHANGELOG.md
Re-run with --no-default-excludes to review them anyway.
```

That exits `0`, not an error — a docs-only PR is a pass, not a failure, and in
CI it leaves a green check rather than a red one. `--no-default-excludes` turns
the built-in set off when the prose itself is what you want reviewed;
`TRI_REVIEW_EXCLUDE` replaces the set with your own. `--exclude` always *adds*
to whatever is in effect.

Being dropped from the payload and being grounds for skipping the PR are two
different claims, and the defaults keep them apart. Prose, snapshots and images
are both — a PR of nothing but those has nothing to triangulate. **Lockfiles are
only the first.** They are machine-written and not worth tokens alongside real
code, but a PR that changes *only* a lockfile is exactly the shape of a
dependency bump, accidental or otherwise, and that is the last thing that should
pass unreviewed. So a lockfile-only PR is reviewed, and says so:

```
Every changed file is excluded from the payload, but 2 of them are not grounds
for skipping a review (dependency or generated files that still record a
decision). Reviewing uv.lock, package-lock.json.
```

Patterns *you* name are taken at face value in both roles: `--exclude '**/*.py'`
on an all-Python PR skips it, because you said so.

Note the gate is strictly path-based. A glob cannot be wrong about whether
`README.md` is Markdown, which is what makes skipping on it safe to do
automatically. Guessing that a change to `deploy.sh` is "only a comment" is a
different problem with a much worse failure mode — silently not reviewing real
code — so `tri-review` does not do it.

**An unchanged PR replays its last review instead of buying a new one.** Each
completed run is stored under `~/.cache/tri-review/`, keyed by repository and PR
number, holding the report and every model's structured findings. Re-run against
the same head commit and you get that report back instantly, for free:

```
$ tri-review --pr 51
No change since the last review of a1b2c3d4 on 2026-09-08T14:02:11+00:00.
Replaying it — pass --fresh to buy a new one.
```

In cwd mode there is a fourth condition, because the file bodies come from your
working tree while the stored review is keyed on the PR's head commit. Replay
requires `HEAD` to *be* that commit with no uncommitted changes — otherwise
review a PR from the wrong branch once and the wrong review is cached under the
right SHA and replayed long after the mistake is fixed. For the same reason a
run from a mismatched tree is not written to history at all. `--repo` and `--url`
mode read contents at the SHA itself, so nothing there can disagree.

The stored run is only reused when the head SHA, the model panel, and the
exclude patterns all still match. Any of them changing makes the old report an
answer to a different question, so it is discarded and the reason is printed.
Push a fix and the SHA moves, so the next run is a real one — which is the
normal loop: review, fix, push, review again.

**Optionally, a model can be asked whether the diff changes behaviour at all.**
Path globs cannot tell a rewrite of `deploy.sh` from a typo fix in a comment
above it. `--triage` puts that question to the cheapest configured model before
the panel runs:

```bash
tri-review --pr 51 --triage
```

It is **off by default, and deliberately so**. It is the only gate that costs a
model call, and the only one that can be wrong — and being wrong here means
silently not reviewing real code, which is the worst thing this tool can do. So
the prompt is asymmetric on purpose: anything uncertain, anything truncated, and
anything that only *looks* like a comment (`# noqa`, `# type: ignore`,
`// eslint-disable`, `#!/usr/bin/env`, build pragmas, framework annotations)
answers "review it". A provider error or an unparseable answer reviews too. When
it does skip, it says out loud that a model decided, and how to override:

```
Nothing to review. Triage (gemini-3.7-flash) found no behaviour change in PR #51:
the only change is a docstring.
This is a model's judgement, not a path rule, so it can be wrong. Re-run with
--no-triage to review anyway.
```

Turn it on for every run with `TRI_REVIEW_TRIAGE=1`, or in CI with the Action's
`triage: true` input. `--no-triage` overrides either.

`--dry-run` never runs triage — it would mean a model call, and a dry run is documented as costing nothing and needing no keys.

### What gets reused

Individual reviewer calls are cached on an exact content hash — the model, the
payload, the reviewer prompt, and the output schema. Not similarity: two diffs
that are 99% alike can differ in exactly the line that introduces the bug, so
"close enough" would mean confidently reviewing code that was never read.

The payoff is the partial retry. When a provider flakes, the reviews that *did*
land are already paid for, and re-running calls only the model that failed. (A
run missing a reviewer is never replayed wholesale from the stored report —
that would reprint the degraded panel and never re-call the model that flaked.)

```
  OK gpt-5.6-terra — 3 findings (cached)
  OK claude-sonnet-5 — 2 findings (cached)
  OK gemini-3.7-flash — 3 findings
```

The key is not scoped by repository, and that is deliberate: the payload holds
the diff and the full text of every file in it, so two repositories can only
collide by containing identical code — where the same review is the right answer
for both. The store lives in your own cache directory, so nothing is shared
between people.

Failures are never stored, which is what makes that retry a real retry. Editing
`REVIEW_PROMPT` or adding a field to `Finding` changes the hash and invalidates
every entry automatically. Entries expire after 14 days
(`TRI_REVIEW_CACHE_TTL_DAYS`, `0` to disable), and the cache directory is safe to
delete at any time. `--fresh` bypasses both this and the stored report.

### Choosing the panel

Repeat `--model` to pick which models review the PR, overriding the configured slots:

```bash
tri-review --pr 123 \
  --model gpt-5.6-terra \
  --model claude-sonnet-5 \
  --model gemini-3.7-flash
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
| `TRI_REVIEW_MODEL_A` | `gpt-5.6-terra` | First reviewer |
| `TRI_REVIEW_MODEL_B` | `claude-sonnet-5` | Second reviewer |
| `TRI_REVIEW_MODEL_C` | `gemini-3.7-flash` | Third reviewer |
| `TRI_REVIEW_SYNTHESIZER` | same as model A | Model that cross-references the reviews |
| `TRI_REVIEW_TOKEN_BUDGET` | `100000` | Max estimated tokens for diff + file context |
| `TRI_REVIEW_TIMEOUT` | `120` | Per-model timeout in seconds |
| `TRI_REVIEW_EXCLUDE` | see `DEFAULT_EXCLUDES` | Comma- or newline-separated globs that replace the built-in skip set |
| `TRI_REVIEW_HISTORY_DIR` | `~/.cache/tri-review` | Where per-PR review history is stored |
| `TRI_REVIEW_CACHE_TTL_DAYS` | `14` | How long a cached reviewer call stays usable; `0` never expires |
| `TRI_REVIEW_TRIAGE` | unset | Set to `1` to run the behaviour-change gate on every run |
| `TRI_REVIEW_TRIAGE_MODEL` | same as model C | Model asked whether the diff changes behaviour |

The provider is chosen from the model ID prefix (`gpt-`, `o1`, `o3`, `o4`, `claude-`,
`gemini`), so
you can point any slot at any supported provider — including three models from the
same provider if you only have one key, with the caveat described above.

`--model` takes precedence over `TRI_REVIEW_MODEL_A/B/C`: use the env vars for your
standing default panel, and the flag for a one-off.

## How it behaves

- **A PR with nothing to review is a success, not an error.** If every changed file is documentation, a lockfile, or generated output, the run stops at exit `0` before any model is called and names what it skipped. Exiting non-zero there would fail a CI check on a docs-only pull request, which is backwards.
- **A retry after a flake only re-calls what failed.** Reviews are cached on an exact content hash, so when a provider drops out and the run exits `4`, re-running reuses the reviews already paid for and buys just the missing one.
- **A model that fails does not sink the run.** If one provider is down, rate-limited, or missing a key, the other two still produce a report and the failure is noted at the top.
- **Fewer than two reviews is an error.** A single-model review is just a code review, so `tri-review` exits non-zero rather than pretending it triangulated anything.
- **Large PRs degrade rather than fail.** If the diff plus changed-file contents exceed the token budget, the largest files' contents are dropped (their diff hunks are kept) and the dropped files are named in a warning. If the diff *alone* busts the budget, that is called out too — no file contents can be included and the providers may reject the payload.
- **Consensus between same-family models is labeled as weak.** If every reviewer that reported came from one provider, the report opens with a banner saying so rather than presenting their agreement as corroboration.
- **The diff is untrusted input.** Paths in it are written by whoever opened the PR, so a diff header pointing outside the repository — via `../` or a symlink the PR adds — is refused and named in the run output instead of being read and shipped to the model providers.

Exit codes: `0` success — including a PR that held nothing worth reviewing, `2` environment problem (no `gh`, not authenticated, not a repo, or a bad flag), `3` no such PR, `4` fewer than two reviews, `130` interrupted.

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
  contents: read
  pull-requests: write
  issues: write

jobs:
  review:
    runs-on: ubuntu-latest
    steps:
      # Pin to the PR's actual head commit -- the default pull_request checkout
      # is a synthetic merge commit, which would pair the diff with the wrong
      # file contents.
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.sha }}
      # @v1 is a mutable tag; pin to a release commit SHA instead if you want
      # the run to be immune to the tag being retargeted.
      - uses: JairoE/tri-review@v1
        with:
          openai-api-key: ${{ secrets.OPENAI_API_KEY }}
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
          google-api-key: ${{ secrets.GOOGLE_API_KEY }}
```

The runner's own checkout already has the PR branch, so this runs the CLI's normal
cwd mode — the same local-checkout path described above, no `--repo`/`--url`
needed. Each run posts its own comment (matched by a hidden marker) and marks
the previous ones outdated, so the PR records what every commit was told rather
than overwriting it. Reviews are a record of what was checked and when; a single
comment edited in place loses which findings were raised against which commit,
and whether a finding was answered or silently disappeared on the next push.

GitHub has no "resolve" for ordinary PR comments — resolvable threads exist only
for comments anchored to a line of the diff, and a whole-PR report is not
anchored to a line. The native equivalent is what the comment menu's **Hide**
does, and that is what the Action calls: the superseded report collapses behind
*"This comment was marked as outdated"*, still open-able, still in the timeline.

Hiding a comment needs more permission than posting one. If the workflow's token
cannot do it, the Action falls back to editing the old report into a collapsed
`<details>` block with a line saying which commit superseded it — the same idea
with the permission it is already known to have. Nothing is ever deleted. Set
`comment-mode: update` to go back to a single comment edited in place, which is
quieter on a long-running PR at the cost of that history.

A PR the gates skip posts a short "Nothing to review" comment and passes, rather than failing the check. Alongside `report-path` and `exit-code`, the action exposes `skipped` (`'true'` when no review was produced) and `skip-reason` (`path`, `empty-diff`, `triage`, or `cap`). The distinction matters, and each reason gets its own comment text: `path` means every changed file matched an exclude glob, `empty-diff` means the diff itself came back empty and is not a claim about what kind of files the PR touches, `triage` means one cheap call was spent reaching a verdict that can be wrong, and `cap` means the PR had already used up `max-reviews` (below). `path`, `empty-diff` and `cap` made no provider call at all.

| Input | Default | Purpose |
|---|---|---|
| `pr-number` | autodetected | Which PR to review; usually left unset |
| `models` | the three configured slots | Space-separated model IDs, same rules as `--model` |
| `exclude` | none | Newline-separated glob patterns, same as `--exclude`. Adds to the built-in skip set |
| `triage` | `false` | Ask the cheapest model whether the diff changes behaviour, and skip the review if it plainly does not |
| `fail-on-insufficient-reviews` | `true` | Whether exit code `4` (fewer than two reviews) fails the check or just posts a warning |
| `max-reviews` | none (no cap) | Most reports to produce on one PR; later runs skip with `skip-reason: cap`. See [Capping reviews per PR](#capping-reviews-per-pr) |
| `force` | `false` | `'true'` ignores `max-reviews` for this run, e.g. `${{ github.event.action == 'labeled' }}` |
| `post-comment` | `true` | Whether to post a PR comment at all |
| `comment-mode` | `append` | `append` posts a comment per run and marks earlier ones outdated; `update` edits one comment in place |
| `github-token` | `${{ github.token }}` | Used for both `gh auth` and posting the comment |
| `openai-api-key` / `anthropic-api-key` / `google-api-key` | none | At least two required |

### Capping reviews per PR

Every review costs three model calls, and a PR that takes ten pushes pays for
ten. `max-reviews` caps that:

```yaml
name: tri-review
on:
  pull_request:
    # `labeled` is what lets someone ask for one more review once the cap is hit.
    types: [opened, synchronize, reopened, labeled]

permissions:
  contents: read
  pull-requests: write
  issues: write

jobs:
  review:
    # Any label starts a `labeled` run; only this one should.
    if: github.event.action != 'labeled' || github.event.label.name == 'tri-review:again'
    # One review per PR at a time -- see "Concurrency" below. On the job, not
    # the workflow: a job skipped by the `if` above never joins the group, so
    # an unrelated label cannot displace a review.
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
          # Adding the label is the explicit request for one more review.
          force: ${{ github.event.action == 'labeled' }}
          openai-api-key: ${{ secrets.OPENAI_API_KEY }}
          anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
          google-api-key: ${{ secrets.GOOGLE_API_KEY }}
      # Take the label back off, so adding it again is the next request. `|| true`
      # because a re-run of this job, or someone removing it by hand, leaves no
      # label to delete, and that must not turn a finished review red.
      - if: always() && github.event.action == 'labeled'
        env:
          GH_TOKEN: ${{ github.token }}
        run: gh api -X DELETE "repos/${{ github.repository }}/issues/${{ github.event.pull_request.number }}/labels/tri-review:again" || true
```

Once the PR already has `max-reviews` reports, the next run skips before it
installs anything or calls any model, reports `skip-reason: cap`, and passes.
`force: true` ignores the cap for that run.

**What counts is a run that produced a report.** "Nothing to review" skips do
not use up the cap, not even `triage` skips (one cheap call, not a review), and
neither do runs that failed before producing a report, or the cap notes below.
Each comment the Action posts records a running count of reports in a hidden
header line, and the cap reads the highest one back. A tally rather than a
count of comments, because `comment-mode: update` edits one comment forever.
Reports posted before v1.1.0 carry no tally and are counted one each; skip and
failure comments from then are recognised by their headline and not counted.
In `append` mode a PR that was open across the upgrade keeps an accurate count.
In `update` mode an older version left one edited comment however many reviews
it held, so such a PR restarts from 1 (or 0, if that comment was last a skip).

**A capped run posts a short note** saying the cap was reached and that this
commit was not reviewed, so a push never looks reviewed when it was not. The
note does not hide the last real report -- that report still stands for the
commit it names -- and consecutive capped pushes edit the one note rather than
adding one each. The next forced review posts normally and marks both outdated.
In `comment-mode: update` this means a capped PR briefly shows two comments,
the report and the note; the next forced review is written into the note and
marks the old report outdated, leaving one again.

Which events start the workflow, which label forces a run, who may add it and
removing it afterwards all stay in your workflow: an Action cannot choose its
own triggers, and a label name is one repo's policy. The Action only counts and
decides.

**Concurrency.** The Action does not lock across runs, so two pushes in quick
succession can both count below the cap before either posts. The job-level
`concurrency` group above prevents that by running one review per PR at a
time. Leave `cancel-in-progress` at `false` when you use a cap: a review is paid
for as soon as its models are called but counted only once its comment is
posted, so cancelling a running review spends the money and loses the count.
It would also let a push cancel a review someone forced with the label. With
`false`, a running review always finishes. GitHub keeps only the *newest*
waiting run per group, so a burst of pushes reviews the one that was running
and the latest one, and skips the ones in between. That is the cheapest
outcome, but those in-between commits get no comment at all.

**When the cap cannot count, it says so rather than guessing.** If the Action
cannot list the PR's comments (a transient API error, a token that cannot read
them), it fails open: that run is reviewed and carries a warning. The cap
counts comments this Action posted, so with `post-comment: false` it can never
be reached, and the run warns about that too. It also counts only comments
posted by the token's own login. A GitHub App installation token cannot report
its login, so reports it posted are not matched and the count reads zero. The
run warns when it sees tri-review comments from another bot and none from
itself. Use the default `github.token` or a personal access token with
`max-reviews`.

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

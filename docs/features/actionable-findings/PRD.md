# Actionable Findings PRD

## Summary

Rework how tri-review finds and reports issues so every finding is short, specific, and fixable:

1. **Sharper findings.** Reviewers are asked to look for the bugs that matter and to give, for each one, the **problem**, the **trigger** that makes it fail, and the **fix**, in a few sentences each. A benchmark of known bugs measures recall before and after the change.
2. **Code highlights.** Each finding names the line range its fix starts at, checked against the PR head. Where a reviewer proposed exact replacement code, the report shows it as a diff.
3. **Apply on GitHub.** In phase 1, findings with a checked, exact fix are posted as inline review comments containing GitHub ```` ```suggestion ```` blocks, so the PR author can click **Commit suggestion**. In phase 2, a maintainer can comment `/tri-review apply F2 F5` and tri-review commits those fixes to the PR branch, including fixes on lines GitHub can't attach a suggestion to.

## Problem and User Value

Today:

- **Findings are uneven.** `REVIEW_PROMPT` asks for "bugs, security, logic and performance", and `Finding.detail` takes free text. Some findings are precise. Others are vague ("may cause issues"), have no stated trigger, and can't be checked without re-reading the diff.
- **Line numbers are guesses.** The payload sends file contents without line numbers, so a reviewer has to work out a line from the diff's hunk headers. Nothing checks the `line` it returns, and wrong lines reach the report.
- **The report is free-form prose.** The synthesizer writes the whole Markdown report itself. Consensus, severity, and the Blocking rules are enforced only by the prompt (`SYNTHESIS_PROMPT`), and nothing in the report is machine-readable. That is why there is no reliable way to point at a line or apply a fix.
- **Fixing is all manual.** A reader has to copy a fix out of prose, find the line, and edit the file by hand.

Value: a reviewer reads less, trusts the lines, and applies most fixes with one click or one comment.

## Goals

- G1. Every reported finding states the problem, the trigger, and the fix, each in 1–3 sentences.
- G2. Recall of known, seeded bugs on the benchmark does not regress, and the release notes report the before/after numbers.
- G3. Every finding that has a location shows a file and a 1-indexed line range on the PR head. The report says whether that range was checked against the code.
- G4. Consensus, severity, and tag invariants are computed in code from the reviewers' structured findings the synthesizer cites. Which findings describe the same bug stays the synthesizer's judgment, bounded by the same-file rule in R4.4.
- G5. A finding with a checked, exact fix inside the diff can be applied from the GitHub UI with one click.
- G6. (Phase 2) A maintainer can apply one or more checked fixes by ID from a PR comment, without checking the branch out.

## Non-goals

- Fixes written by a model at apply time. Phase 2 applies only the `replacement_code` that a reviewer produced and the report showed. A finding without one is refused with a reason.
- One finding whose fix spans several files. A finding has one file and one contiguous line range.
- Running tests, linters, or any PR code before or after applying a fix.
- Applying fixes to PRs from forks (the token can't push there). Suggestions still work on forks wherever the Action can post comments.
- Approving or requesting changes. Inline reviews are always posted with event `COMMENT`.
- Changes to the web app (`web/`).
- Multi-pass or agentic reviewing (for example, a second "verify each finding" call per reviewer). That's a candidate follow-up if the benchmark shows a precision problem.

## Users and Primary Flows

**PR author (Action user).** Pushes a commit, then reads the tri-review report comment. Each finding has an ID (`F1`, `F2`, …), a tag, a location link, and Problem/Trigger/Fix. Consensus findings with a checked fix also show up as inline comments on the changed lines, and the author clicks **Commit suggestion**.

**Maintainer (phase 2).** Reads the report and comments `/tri-review apply F1 F3` on the PR. tri-review reacts 👀, commits both fixes to the PR branch in one commit, and replies with the commit link and a per-ID result.

**CLI user.** Runs `tri-review --repo o/n --pr 12` and gets the same per-finding report in the terminal. `--findings-json out.json` writes the machine-readable findings. `--suggest consensus` posts the inline suggestions from the CLI.

## Requirements

### R1. Reviewer finding schema

R1.1 `Finding` (the schema reviewers are shown) has these fields:

| Field | Type | Meaning |
|---|---|---|
| `file` | str | Repository-relative path |
| `line` | int \| null | First line, 1-indexed on the PR head (new side), of the code the fix changes. Null only if the finding is not tied to a line |
| `end_line` | int \| null | Last line of that range, inclusive. Null means the same as `line` |
| `severity` | critical \| major \| minor | Rubric in R2.4 |
| `category` | bug \| security \| performance \| logic | Unchanged |
| `title` | str | One line, at most 100 characters |
| `problem` | str | What is wrong and what goes wrong because of it. 1–3 sentences |
| `trigger` | str | The concrete input, state, or sequence of calls that makes it fail. 1–2 sentences |
| `fix` | str | The change to make, in words. 1–3 sentences |
| `original_code` | str \| null | The exact current text of lines `line`..`end_line`, copied without the line-number prefix |
| `replacement_code` | str \| null | The exact text that should replace those lines. `""` means delete them. Null when the fix is not a local edit of that range |

R1.2 `detail` and `suggested_fix` are removed. No compatibility shim is needed: the reviewer cache key includes the schema fingerprint (`nodes._output_fingerprint`), and history records are versioned (R8.1).

R1.3 Missing `title`: filled from the first sentence of `problem`, the same way `_title_from_detail` uses `detail` today.

R1.4 Missing or blank `trigger`: the finding is kept with `trigger = ""`, not set aside as malformed. The report shows "Trigger not stated", and the finding can't be tagged Blocking (R4.4).

R1.5 Sentence limits are asked for in the field descriptions and the prompt, and nothing enforces them. A long finding is kept whole: never truncated, never rejected.

### R2. Reviewer prompt and payload

R2.1 **Numbered file contents.** Each `## Current contents of <path>` block in the payload has every line prefixed with its 1-indexed line number, right-aligned to the width of the largest number, followed by ` | `. Example: `  42 | return x`.

R2.2 **Numbered diff.** Each added (`+`) and context (` `) line in the payload's diff gets its new-side line number in the same format. Removed (`-`) lines get blank padding of the same width. Hunk headers and file headers are unchanged.

R2.3 The prompt tells reviewers that `line`/`end_line`/`original_code` must come from those numbers and that text, and that the prefix is not part of the code.

R2.4 `REVIEW_PROMPT` is rewritten to:

- Name what to hunt for: wrong results on reachable inputs, unhandled error and empty/null paths, boundary and off-by-one conditions, broken contracts with callers or callees visible in the payload, resource leaks, concurrency and ordering, injection and trust-boundary mistakes, and changes in behaviour that callers rely on.
- Require a trigger. A finding whose trigger the reviewer can't state is not reported. A trigger may be a condition ("when `items` is empty") rather than a full reproduction.
- Give a severity rubric. **critical**: data loss, a security hole, or a crash on a common path. **major**: a wrong result or crash on a plausible path. **minor**: a real defect on an unlikely path, or a robustness gap.
- Exclude style, naming, comments and docs, "consider adding logging/tests", and speculative refactors.
- Ask for `original_code`/`replacement_code` only when the fix is a contiguous edit of one range in one file, and ask for whole lines with their indentation.
- Keep today's rules: review the change, not the whole file, and don't give an empty findings list without checking every changed hunk.

R2.5 Token budget: the numbered payload is the one `context.ReviewContext.render()` returns, so `estimated_tokens` and the budget-dropping logic cover the extra characters automatically.

R2.6 Prompt caching (#40) is unaffected in design. The automatic breakpoint still caches tools + system + payload as one entry. But the reasoning comment in `providers.build_llm` cites "tool schema plus REVIEW_PROMPT is ~460 tokens", and the v2 schema and prompt make that number wrong. Re-measure it and update the comment.

### R3. Anchors: checking line numbers

R3.1 After each reviewer returns, and for cached results too, every finding gets an **anchor**: `status` (`verified` \| `relocated` \| `unverified`), the final `line`/`end_line`, and `in_diff` (bool). The anchor is not part of the schema reviewers are shown (`SkipJsonSchema`, as with `ReviewOutput.malformed`).

R3.2 The **source text** for `file` is its full contents from the review context when present. Otherwise, when the file was dropped for budget, it's the new-side lines in its diff hunks, which are known by line number.

R3.3 Comparing two pieces of code means comparing them line by line, after CRLF→LF and stripping trailing whitespace from each line.

R3.4 Resolution, in order:

1. No `original_code` or no source text → `unverified`, keeping the reported line.
2. `line` is set and the source lines `line`..`end_line` equal `original_code` → `verified`.
3. `original_code` occurs exactly once in the source → `relocated`, with `line`/`end_line` moved to that occurrence.
4. It occurs more than once → `relocated` to the occurrence whose start is nearest the reported `line`. A tie for nearest, or no reported `line`, → `unverified`.
5. Otherwise → `unverified`, keeping the reported line.

R3.5 `in_diff` is true when every line of the final range is an added or context line on the new side of **one** diff hunk of that file.

R3.6 A finding is **suggestable** when its anchor is `verified` or `relocated`, `in_diff` is true, and `replacement_code` is not null.

R3.7 A finding is **applicable** (phase 2) when its anchor is `verified` or `relocated` and `replacement_code` is not null. `in_diff` is not required.

### R4. Structured synthesis

R4.1 The synthesizer is called with structured output (`SynthesisOutput`) instead of returning Markdown.

R4.2 Its input labels each reviewer with a letter in panel order (`A`, `B`, `C`, …) and each finding with a 1-based ref (`A1`, `A2`, `B1`). Each finding is sent with its anchor.

R4.3 `SynthesisOutput.findings[]` has these fields:

| Field | Meaning |
|---|---|
| `sources` | Reviewer refs this finding merges. Empty only for an issue the synthesizer adds itself |
| `tag` | `blocking` \| `blocking_pending_check` \| `optional` |
| `title`, `problem`, `trigger`, `fix` | Merged wording, under the same sentence limits as R1.1 |
| `check` | For `blocking_pending_check`: the one check that settles it |
| `falsified_by` | Required for `blocking` and `blocking_pending_check` |
| `dismissed_reason` | The recorded reason, when this matches a previously rejected claim |
| `new_evidence` | What in this diff the earlier rejection didn't cover, if anything |

R4.4 Code then derives or enforces the following. The synthesizer's own values for these are ignored or overridden.

- **Unknown refs** in `sources` are dropped, and a note lists them. A finding left with no sources counts as synthesizer-added.
- **`reported_by`**: the distinct models behind `sources`.
- **`consensus`**: two or more distinct models in `reported_by` whose source findings name the **same file**. Sources spread over different files are still merged into one finding, but it isn't consensus. This is a floor, not a full check: whether two same-file findings describe one bug is still the synthesizer's judgment. A wrong merge can only make a finding look corroborated within one file.
- **`severity`**: the highest severity among the sources. Synthesizer-added findings are `minor`.
- **`category`, location, anchor, `original_code`, `replacement_code`**: taken from the **primary source**. That is the first source, in panel order, whose anchor is `verified`/`relocated` and `in_diff`. Failing that, the first `verified`/`relocated` source. Failing that, the first source.
- **Tag downgrades.** `blocking` becomes `blocking_pending_check` if `check` is set. Otherwise it becomes `optional` unless `consensus` is true, `severity` is critical or major, `trigger` is not blank, and `falsified_by` is not blank. `blocking_pending_check` with a blank `check` or `falsified_by` becomes `optional`. A finding with a `dismissed_reason` and no `new_evidence` becomes `optional`. A synthesizer-added finding is `optional`.
- **Never hide a finding.** Every reviewer finding not cited by any synthesized finding is appended as its own finding (tag `optional`, the reviewer's own wording, `reported_by` that model). The report notes how many were added this way.

R4.5 **IDs.** Findings are sorted by tag (blocking, then blocking_pending_check, then optional), then severity, then whether they're consensus findings, then file, then line. They are numbered `F1`…`Fn` in that order. IDs are stable within one report, not across reports.

R4.6 **Fallback.** If the structured synthesis call fails, the report is built from the reviewer findings with no merging. Each finding is its own `F` entry, tagged `optional`, and the report opens with the existing "Synthesis failed" note. As today, the run is not stored in history (`synthesized = False`).

### R5. Report rendering

R5.1 The report Markdown is rendered by code (new `tri_review/report.py`) from the derived findings. The header notes are unchanged: panel line, single reviewer, failures, provider diversity, low confidence, malformed findings.

R5.2 Below the notes there's a summary line, for example `**2 blocking · 1 needs a check · 4 optional**`, then three sections with the existing names:

- `## Consensus Findings`: the findings where `consensus` is true.
- `## Unique Insights`: the rest, each attributed to its reporting model as "(unverified by the other reviewers)".
- `## Actionable Next Steps`: one line per finding in ID order: tag, ID, title, location. No prose.

R5.3 Each finding renders as follows. The location is a link only when the repo and head SHA are known; otherwise it's plain code text.

````markdown
### F1 · Blocking · **major** — Retry loop never stops on HTTP 429
[`src/app/client.py:42-44`](https://github.com/o/n/blob/<head_sha>/src/app/client.py#L42-L44) · gpt-6-sol@high, claude-sonnet-5

**Problem:** …
**Trigger:** …
**Fix:** …

```diff
-        while resp.status == 429:
+        for _ in range(MAX_RETRIES):
```

_Falsified by: …_
````

- The diff block appears only when `original_code` and `replacement_code` are both present and the anchor is `verified` or `relocated`.
- An `unverified` location shows `(line not verified)` after it.
- `blocking_pending_check` shows `**Check:** …`.
- A dismissed finding shows `_Previously dismissed: <reason>_`.
- A finding with no location shows the file alone.

R5.4 Model-written text (every field the reviewers or the synthesizer produced) is escaped before rendering, so it can't open an HTML comment (`<!--` → `&lt;!--`) or close a code fence the renderer opened. Code blocks use a fence longer than any backtick run inside them.

R5.5 `render_findings_md` (the per-model view) and the raw-findings fallback render the new fields the same way.

### R6. Findings JSON

R6.1 New CLI flag `--findings-json FILE` writes:

```json
{
  "version": 1,
  "repo": "owner/name or null",
  "pr": "123",
  "head_sha": "40-hex or empty",
  "synthesized": true,
  "findings": [
    {
      "id": "F1", "tag": "blocking", "severity": "major", "category": "bug",
      "consensus": true, "reported_by": ["gpt-6-sol@high", "claude-sonnet-5"],
      "title": "…", "problem": "…", "trigger": "…", "fix": "…",
      "check": null, "falsified_by": "…", "dismissed_reason": null,
      "file": "src/app/client.py", "line": 42, "end_line": 44,
      "anchor": "verified", "in_diff": true,
      "original_code": "…", "replacement_code": "…",
      "suggestable": true, "applicable": true
    }
  ]
}
```

R6.2 It's written whenever a report is written, including a replayed report (R8.2) and the synthesis-failure fallback. A write failure is a warning, like `--output` today.

### R7. Inline suggestions (phase 1)

R7.1 New CLI flag `--suggest none|consensus|all` (default `none`) and Action input `inline-suggestions` (default `consensus`). Under `consensus`, the suggestable findings where `consensus` is true are eligible. Under `all`, every suggestable finding is.

R7.2 New Action input `max-suggestions` (default `15`) and CLI flag `--max-suggestions N`. Eligible findings are taken in ID order up to the cap, and the report comment says how many were left out.

R7.3 Posting creates one PR review via `gh api POST repos/{repo}/pulls/{pr}/reviews` with:

- `commit_id` = the reviewed head SHA, and `event` = `COMMENT`.
- A one-line body pointing readers to the tri-review report comment for the full review. It doesn't link to the comment, because the Action posts the report comment after the suggestions.
- One comment per eligible finding: `path`, `line` = `end_line`, `side` = `RIGHT`, plus `start_line`/`start_side` when the range spans more than one line.

R7.4 Each comment body contains:

- A hidden marker on its first line: `<!-- tri-review-suggestion:<fp> -->`, where `<fp>` is the first 16 hex characters of SHA-256 over `path`, the anchored `line` and `end_line`, and the normalized `original_code` (R3.3). `replacement_code` is deliberately left out. Models word the same fix differently on every run, so including it would post a fresh comment on the same lines after every push.
- A title line: `**F1 · Blocking · major** — <title>`.
- Problem, Trigger, and Fix.
- A ```` ```suggestion ```` block containing `replacement_code`, fenced longer than any backtick run inside it.

R7.5 **Dedup.** Before posting, tri-review lists the PR's existing review comments (paginated) by the posting identity, resolved the same way as `scripts/pr_comment.sh`'s `resolve_bot_login`. It skips any eligible finding whose marker `<fp>` already appears there. If nothing is left to post, no review is created.

R7.6 **Errors.**

- If the review POST returns 422, tri-review posts each comment on its own (`POST repos/{repo}/pulls/{pr}/comments`) and skips the ones that fail. Any other failure skips posting entirely.
- Either way, the run's exit code is unchanged, and stderr names what wasn't posted.
- The CLI refuses to post when `working_tree_reason` is set (cwd mode with a working tree that isn't the head SHA) and prints why.

R7.7 `--dry-run` never posts. A run whose review was skipped (path, triage, cap, or empty diff) posts nothing.

### R8. History and replay

R8.1 `history.SCHEMA_VERSION` becomes `2`. `RunRecord` gains `findings` (the R6.1 `findings` list). Version-1 records are read as a cache miss, as unknown versions are today.

R8.2 A replayed report writes `--findings-json` from the stored findings, and posts suggestions if asked. Dedup (R7.5) makes reposting a no-op.

### R9. Apply command (phase 2)

R9.1 New console script `tri-review-apply`, with no model calls and no provider keys:

```
tri-review-apply --repo owner/name --pr N --comment-id ID --actor LOGIN --body-file FILE
```

R9.2 **Grammar.** The first line of the comment, trimmed and matched case-insensitively, is `/tri-review apply` followed by one or more IDs (`F` plus digits), separated by spaces or commas. Anything else gets a reply explaining the usage, and nothing is applied.

R9.3 **Authorization.** `gh api repos/{repo}/collaborators/{actor}/permission` must return `admin`, `maintain`, or `write`. Otherwise tri-review replies with a refusal and applies nothing.

R9.4 **Forks.** If the PR's head repository is not the base repository, tri-review replies that fixes can't be pushed to a fork and that suggestions can be used instead. Nothing is applied.

R9.5 **Findings source.** Take the newest PR comment that:

- is authored by the posting identity,
- starts with the report marker (`<!-- tri-review-report -->`), and
- is a **report**, not a cap note (`<!-- tri-review-cap -->`), a "Nothing to review" skip, or a "failed to produce a report" comment. These kinds are told apart the same way `count_reviews` in `scripts/pr_comment.sh` does.

Its **last line** must be `<!-- tri-review-findings:v1 <base64 JSON> -->`. If that newest report has no block (it was left out for size, or it has no applicable findings), reply saying so. Never fall back to an older report. The JSON holds `head_sha` and, for every applicable finding: `id`, `title`, `file`, `line`, `end_line`, `original_code`, `replacement_code`. The Action appends this line after all model-written text. Model-written text can't contain a raw `<!--` (R5.4).

R9.6 **Per-ID checks**, against the file at the PR's current head:

- The ID must be in the block. Otherwise: "F9 is not in the latest report".
- It must be applicable. Otherwise: "F3 has no exact fix to apply".
- Its `original_code` must still be found, using R3.4 steps 2–4 against the current file. Otherwise: "the code F2 changes has moved or changed since the review".
- Two requested ranges in the same file must not overlap. If they do, the later ID is refused.
- A file under `.github/workflows/` is refused: "F4 edits a workflow file, which the workflow token can't push". GitHub rejects such pushes without the `workflows` permission, and granting it is out of scope.
- A file `fetch_file_content` can't read (binary, over 1 MB, or missing at head) is refused: "F6's file can't be read at the PR head".

R9.7 **Commit.** The accepted replacements are applied bottom-up within each file, in one commit, using the Git Data API (blobs → tree → commit → `PATCH refs/heads/<branch>` with `force: false`). Nothing is checked out and no PR code is run. Each tree entry keeps the **mode** the file has in the head commit's tree (for example `100755` for an executable script), read with `GET git/trees/<head sha>?recursive=1`. Writing every file as `100644` would quietly drop the executable bit. The commit:

- Is titled `tri-review: apply F1, F3`.
- Has a body with one line per ID (title and location), plus `Requested-by: @<actor> (<comment URL>)`.

If the ref update fails because the branch moved, tri-review replies "the branch moved while applying; run the command again" and nothing is committed.

R9.8 **Replies.** tri-review adds a 👀 reaction to the command comment when it starts. When it finishes, it posts one reply listing each ID as applied or refused (with the reason), the commit link if any, and a note that a commit pushed by the workflow token doesn't start a new review: push again, or add `tri-review:again`.

R9.9 **Exit codes.** `0` when the command was handled and a reply posted, whether or not anything was applied. `2` for preflight or usage errors. `1` when a GitHub write (commit or reply) failed.

R9.10 **Shipping.** Delivered as a second composite action at `apply/action.yml` (`JairoE/tri-review/apply@v1`). In this repo, the apply workflow follows the same rule as `.github/workflows/tri-review.yml`: `uses:` a SHA already on main, never `./apply`. It checks out nothing. It runs `tri-review-apply` with the comment fields passed through `env`, never interpolated into shell. The README documents a consumer workflow:

- Trigger: `issue_comment: [created]`.
- `if`: the issue is a PR and the comment starts with `/tri-review apply`.
- Permissions: `contents: write`, `pull-requests: write`, `issues: write`.
- `concurrency` group per PR.

### R10. Benchmark

R10.1 New script `scripts/bench.py`, run by hand and never in CI. It reads `bench/cases.toml`. Each case is a commit in this repo whose message starts with `fix:`. The case's "PR" is the reverse of that commit's changes under `src/` (`git diff <fix> <fix>^ -- src/`), so it puts the bug back. Tests are left out so they can't give the bug away.

R10.2 **Ground truth** is the bug's location on the new side of that reverse diff. That means the new-side line of each added line, and for a run of removed lines, the new-side line where they were.

R10.3 For each case and each reviewer, the script builds the payload with `context.build_context` (the files read at `<fix>^`), calls `nodes.review_with`, and records a **hit** if any finding names a file in the reverse diff and its range is within 3 lines of a ground-truth line. The range is the anchored one (R3) where anchors exist and the reported `line`..`end_line` before they do, so the script also runs at the baseline commit.

R10.4 Output is a Markdown table with:

- Per-reviewer recall (cases hit / cases).
- Panel recall (a case hit by at least one reviewer).
- Consensus recall (a case hit by two or more).
- Findings per case.
- Median words per finding.
- The share of findings whose anchor is `verified` or `relocated`.

R10.5 The baseline is run on the commit before the prompt change (R2) and committed as `bench/results/baseline.md`. The prompt-change PR commits `bench/results/after-prompt.md`.

## UX and Content

- Labels in the report: `Blocking`, `Needs a check` (for `blocking_pending_check`), and `Optional`. Severity is bold lowercase, as today.
- The words "Problem", "Trigger", and "Fix" are fixed labels. When the trigger is missing, the text reads "Trigger not stated".
- Inline comments repeat the finding ID so they can be matched to the report and to `/tri-review apply`.
- The terminal (Rich) renders the same Markdown. The hidden findings block is never part of the CLI's own report: the Action adds it to the comment.

## Data and API Contract

- **Reviewer schema** `Finding`: R1.1. **Anchor**: R3.1. **Synthesizer schema** `SynthesisOutput`: R4.3. **Findings JSON**: R6.1, version 1. **Hidden block**: R9.5, version 1.
- **GitHub endpoints**, all through `gh api`:
  - `POST pulls/{n}/reviews`, `POST pulls/{n}/comments`, `GET pulls/{n}/comments`
  - `GET collaborators/{u}/permission`
  - `GET issues/{n}/comments`, `POST issues/{n}/comments`, `POST issues/comments/{id}/reactions`
  - `GET contents/{path}?ref=`, `GET git/trees/{sha}?recursive=1`, `POST git/blobs`, `POST git/trees`, `POST git/commits`, `PATCH git/refs/heads/{branch}`, `GET pulls/{n}`
- **New CLI flags**: `--findings-json FILE`, `--suggest MODE`, `--max-suggestions N`. **New Action inputs**: `inline-suggestions`, `max-suggestions`. **New Action output**: `findings-path`.

## Rules and Edge Cases

- **Line numbers are on the PR head** (the new side), 1-indexed, with inclusive ranges, everywhere: schema, JSON, links, and GitHub comments.
- **Deleted files** have no new side. A finding on one is `unverified`, can't be suggested, and can't be applied.
- **Renamed files**: the path is the new path (`+++ b/`), as in `context.parse_changed_files`.
- **Fallback lines**: a file whose contents were dropped for budget can still be anchored against its hunk lines (R3.2). A range that reaches outside those lines is `unverified`.
- **Empty replacements**: `replacement_code = ""` deletes the range. It is rendered as an empty suggestion block, and as a diff with only `-` lines.
- **Trailing newlines**: replacement text is applied as whole lines. A replacement with no trailing newline gets one, so the next line is never merged into it.
- **Synthesizer and structured output**: models that reject forced tool calls (for example `claude-sonnet-5-5`, which returns 400 on v1.2.1) now fail as synthesizers and fall back to R4.6. The README says so.
- **The PR moves mid-run**: suggestions are posted against the reviewed `commit_id`. GitHub shows them as outdated if the lines changed. Apply re-checks against the current head (R9.6).
- **Prompt injection from the PR**: reviewers read text written by the PR author, so model-written replacement code may have been steered by it. Mitigations:
  - Every replacement is shown verbatim in the report and in the suggestion before anyone can apply it.
  - Apply needs a human with write access to name the IDs.
  - Apply never runs code.
  - Model text can't forge the hidden block (R5.4, R9.5).
- **Comment size**: GitHub bodies are limited to 65,536 characters. If the report plus the hidden block would exceed 65,000, the block is left out and the report says fixes can't be applied by command for this report. An inline comment over the limit is skipped and noted.

## Acceptance Criteria

1. **Shape.** On a real PR, every finding in the report has a non-blank Problem and Fix, and either a Trigger or "Trigger not stated". (Unit: renderer. Live: a CLI run from the implementing branch, `tri-review --repo JairoE/tri-review --pr <n>`. This repo's CI can't show it before merge, because CI runs the action pinned to a SHA on main, never a PR branch's code.)
2. **Anchors.** Unit tests cover all five steps of R3.4, both sources in R3.2, and the `in_diff` boundaries: the first and last line of a hunk, and a range spanning two hunks gives `false`.
3. **Invariants.** Unit tests show that the synthesizer can't produce:
   - `consensus` from a single model's sources,
   - a severity higher or lower than its sources,
   - Blocking for a minor or single-model finding, or one with a blank trigger or falsifier,
   - a reviewer finding missing from the report.
4. **Benchmark.** `bench/results/after-prompt.md` panel recall ≥ `baseline.md` panel recall, on the same panel and cases.
5. **Suggestions.** With a stub `gh`:
   - `--suggest consensus` sends one review whose comments are exactly the suggestable consensus findings, with correct `line`/`start_line`/`side`.
   - A second identical run sends nothing.
   - A 422 falls back to per-comment posting.
   - cwd mode with a dirty tree refuses.
6. **Live suggestion.** On a PR in this repo, run the CLI from the implementing branch with `--suggest all`. Clicking **Commit suggestion** on one of its inline comments produces a diff equal to the finding's `original_code` → `replacement_code`. CI posts suggestions only after merge and a pin bump.
7. **Apply.** With a stub `gh`:
   - An unauthorized actor, a fork PR, an unknown ID, a non-applicable ID, moved code, overlapping ranges, and a moved branch each produce the specified reply and no commit.
   - A valid `/tri-review apply F1 F2` produces one commit whose tree changes only those ranges.
8. **Replay.** A replayed v2 record writes the same findings JSON as the original run. A v1 record triggers a fresh review.
9. **Injection.** A finding whose `problem` contains `<!-- tri-review-findings:v1 … -->` renders escaped, and `tri-review-apply` ignores it.

## Observability and Privacy

- The CLI prints how many findings were suggestable and applicable, how many suggestions were posted or skipped (deduped, capped, failed), and how many reviewer findings were appended because the synthesizer didn't cite them.
- The Action exposes `findings-path`.
- No new data leaves the runner: the findings come from the same model calls, and GitHub receives what the report already contains.

## Rollout or Migration

- **Phase 1 → v1.3.0**: R1–R8 and R10.
  - Report format changes: IDs, Problem/Trigger/Fix, diff blocks, a compact "Actionable Next Steps". Anyone parsing `report.md` should switch to `--findings-json`.
  - Existing Action users who move with the `v1` tag start getting inline suggestions on consensus findings (default `consensus`). Release notes say so and show `inline-suggestions: none`.
  - Local caches miss once (schema fingerprint and history version change).
- **Phase 2 → v1.4.0**: R9. Opt-in: it does nothing until a repository adds the `issue_comment` workflow.
- This repo's own `.github/workflows/tri-review.yml` pins a SHA on main (currently `d863f33`, main after #40). Bump it after each phase merges, as #41 did, so CI reviews with the new format. Until then, this repo's CI keeps posting the old report.
- `issue_comment` workflows always run from the default branch, so the phase 2 apply workflow can't be tried on a PR before it's on main. Its live check happens after merge.

## Open Questions

1. **Default `inline-suggestions` for the Action.** `consensus` (proposed) or `none`, so existing users opt in?
2. **Benchmark gate.** Is "no regression in panel recall" (AC 4) enough to ship, or should there be a target, such as +2 cases on a 10–12 case set?
3. **Benchmark cases.** Proposed: 10–12 `fix:` commits that touch only `src/`, from `f3e4466`, `b4ca20b`, `c7b174b`, `a1433ca`, `4eb106b`, `253062b`, `220ab41`, `8707639`, `dfd7488`, `41ffe16`, `8190358`, and `63603d1`. Only cases whose reverse diff under `src/` is non-empty are kept. Add cases from other repositories?
4. **Bulk apply.** Should phase 2 accept `/tri-review apply blocking` as well as explicit IDs? It's left out for now so that a human names every change.

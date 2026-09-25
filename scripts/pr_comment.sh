#!/usr/bin/env bash
# Post tri-review's report to a PR, and decide what happens to the last one.
#
# Extracted from action.yml rather than grown in place: this is the only part of
# the Action that mutates something outside the runner, it has two modes and a
# fallback path, and inline composite-action shell cannot be run or tested
# without pushing a commit. Everything here is driven by environment variables
# so it can be exercised against a stub `gh` on PATH -- see tests/test_pr_comment.py.
#
# Required env: GH_TOKEN REPO PR_NUMBER BODY_FILE MARKER MODE
# Optional env: RUN_URL COMMIT_SHA KIND
#
# KIND=cap marks BODY_FILE as a "review cap reached" note rather than a report
# (see review_cap.sh), which changes what happens to the comments before it.
#
# Also sourced by review_cap.sh for its comment-listing helpers; main only runs
# when this file is executed directly.
set -euo pipefail

MODE="${MODE:-append}"
MARKER="${MARKER:-<!-- tri-review-report -->}"
SUPERSEDED_MARKER='<!-- tri-review-superseded -->'
CAP_MARKER='<!-- tri-review-cap -->'

# Scope the marker match to our own posting identity -- otherwise anyone who can
# comment on the PR could craft a comment starting with the same marker and have
# it treated as "the" report comment on the next run.
#
# `gh api user` fails for the default GITHUB_TOKEN (a bot identity, not a real
# user), which is exactly when the well-known bot login is correct; a custom
# github-token (a PAT) resolves to its own real login instead.
#
# The fallback stays OUTSIDE the substitution. Inside it -- `$(cmd || echo
# fallback)` -- both halves' stdout are captured, and `gh api` prints its error
# body to stdout, not stderr: a 403 produced the literal
# `{"message":"Resource not accessible by integration",...}github-actions[bot]`
# and broke the jq filter below with `unexpected token "message"`.
resolve_bot_login() {
  local login
  login=$(gh api user --jq '.login' 2>/dev/null) || login=""
  [ -n "$login" ] || login="github-actions[bot]"
  printf '%s' "$login"
}

# Our prior report comments, oldest first, as `id<TAB>node_id<TAB>base64(body)`.
# The body rides along from this one listing call so nothing downstream needs a
# second GET per comment -- a PR with a long review history would otherwise pay
# for that on every single run, forever, once the token lacks `issues: write`
# (see collapse_comment).
prior_comments() {
  local login="$1"
  gh api "repos/$REPO/issues/$PR_NUMBER/comments" --paginate --jq \
    ".[] | select(.user.login == \"$login\") | select(.body | startswith(\"$MARKER\")) | [.id, .node_id, (.body | @base64)] | @tsv"
}

# Print the hidden marker lines tri-review wrote at the top of a comment body,
# stopping at the first line of anything else -- the report itself, or a skip
# headline, which is printed as `content:<line>` so a caller can tell legacy
# comment kinds apart. Blank lines, the "_Posted by" attribution and the
# summary line collapse_comment wraps a folded report in are stepped over, so a
# folded comment yields the same header as the comment it folded.
#
# Stopping at the first foreign line is the point: a report can quote any text
# at all from the diff it reviewed -- including these very markers, when the
# diff is this repository -- and nothing a report says may be read as ours.
comment_header() {
  printf '%s\n' "$1" | awk '
    /^<!-- tri-review-[a-z]+(:[0-9]+)? -->$/ { print; next }
    /^[[:space:]]*$/ || /^_Posted by / || /^<details><summary>Superseded by / { next }
    { print "content:" $0; exit }
  '
}

# `gh api -f key=@file` does not read from a file -- only `--input` does, and
# that replaces the whole request body. Build the JSON ourselves so a report full
# of backticks, quotes and newlines cannot break the request.
post_comment() {
  jq -n --rawfile body "$BODY_FILE" '{body: $body}' \
    | gh api "repos/$REPO/issues/$PR_NUMBER/comments" -X POST --input - >/dev/null
}

patch_comment() {
  jq -n --rawfile body "$BODY_FILE" '{body: $body}' \
    | gh api "repos/$REPO/issues/comments/$1" -X PATCH --input - >/dev/null
}

# Of the given node ids, print those GitHub does not already consider hidden.
# Without this every run would re-minimize every previous run's comment: harmless
# but a mutation per comment per commit, growing with the length of the PR.
#
# GitHub's node(ids:) lookup accepts at most 100 ids per call -- silently fewer
# results, not an error, if handed more -- so a PR with a longer review history
# than that is batched rather than truncated. The `-f "ids[]=$id"` repeated-flag
# form of building a GraphQL list argument was checked against the live API
# before relying on it here (a single node id round-tripped through `gh api
# graphql` with real comment ids on this repo's own PR history).
not_yet_minimized() {
  local all=("$@") start=0 batch id args
  [ ${#all[@]} -eq 0 ] && return 0
  while [ "$start" -lt "${#all[@]}" ]; do
    batch=("${all[@]:$start:100}")
    args=()
    for id in "${batch[@]}"; do args+=(-f "ids[]=$id"); done
    gh api graphql "${args[@]}" -f query='
      query($ids: [ID!]!) {
        nodes(ids: $ids) { ... on IssueComment { id isMinimized } }
      }' --jq '.data.nodes[] | select(.isMinimized == false) | .id' 2>/dev/null || true
    start=$((start + 100))
  done
}

# GitHub has no "resolve" for issue comments -- resolvable threads exist only for
# file-anchored review comments, and a whole-PR report is not anchored to a line.
# `minimizeComment` is the native equivalent: it is what the UI's "Hide" menu
# calls, and it collapses the comment behind "This comment was marked as
# outdated" while leaving it, and its history, on the PR.
minimize_comment() {
  gh api graphql -f id="$1" -f query='
    mutation($id: ID!) {
      minimizeComment(input: {subjectId: $id, classifier: OUTDATED}) {
        minimizedComment { isMinimized }
      }
    }' >/dev/null 2>&1
}

# Used only when minimize is refused -- minimizing needs more than the
# `pull-requests: write` that posting needs, and a token that cannot do it must
# still not leave two reports looking equally current. Folding the old body into
# a <details> is the degraded form of the same idea, done with the PATCH
# permission we are already known to have. The body is passed in rather than
# fetched here -- see prior_comments and reconcile_older -- but the check stays
# as a guard in case a future caller ever invokes this directly.
collapse_comment() {
  local id="$1" body="$2" superseded
  case "$body" in *"$SUPERSEDED_MARKER"*) return 0 ;; esac

  superseded="Superseded by the review of \`${COMMIT_SHA:-a later commit}\`"
  [ -n "${RUN_URL:-}" ] && superseded="$superseded ([run]($RUN_URL))"

  {
    printf '%s\n%s\n\n' "$MARKER" "$SUPERSEDED_MARKER"
    printf '<details><summary>%s. Click to expand the earlier report.</summary>\n\n' "$superseded"
    # Strip the leading marker so it does not appear inside the fold as text.
    printf '%s\n' "$body" | tail -n +2
    printf '\n</details>\n'
  } > "$BODY_FILE.superseded"

  jq -n --rawfile body "$BODY_FILE.superseded" '{body: $body}' \
    | gh api "repos/$REPO/issues/comments/$id" -X PATCH --input - >/dev/null
}

# Hide every comment described by oldest-first `id<TAB>node_id<TAB>base64(body)`
# lines on stdin: minimize it natively where the token allows, otherwise fold
# it. Shared by append mode (reconciling every prior report) and update mode
# (reconciling append-mode history left behind by a mode switch).
#
# Reads its own input rather than taking array arguments -- bash namerefs
# (`local -n`) need bash 4.3+, and this script's own shebang is `env bash`:
# a consumer running the Action on a macOS runner, or testing it against the
# system bash on a Mac, gets bash 3.2, which does not have them.
#
# A comment already folded into a <details> block is skipped entirely -- no
# GraphQL retry, no PATCH -- rather than re-attempted every run. That check is a
# plain string match on the body already in hand, so it costs nothing, and it is
# a one-way trip: this script has no way to notice a permission grant after the
# fact and go back to natively minimize something already folded. Given the
# choice between that and paying for a failing mutation on every historical
# comment on every future run, the folded comment staying folded is the
# better trade.
reconcile_older() {
  local pending_ids=() pending_nodes=() pending_bodies=()
  local id node_id body_b64 body

  while IFS=$'\t' read -r id node_id body_b64; do
    [ -n "$id" ] || continue
    body=$(printf '%s' "$body_b64" | base64 --decode 2>/dev/null || true)
    case "$body" in *"$SUPERSEDED_MARKER"*) continue ;; esac
    pending_ids+=("$id")
    pending_nodes+=("$node_id")
    pending_bodies+=("$body_b64")
  done
  [ ${#pending_nodes[@]} -eq 0 ] && return 0

  local target j
  while read -r target; do
    [ -n "$target" ] || continue
    # One retry before treating this as a permission problem -- a rate limit
    # or a momentary API hiccup is not the same fact as the token lacking
    # `issues: write`, and folding is a one-way trip (see above), so a
    # transient failure should not spend it.
    if minimize_comment "$target" || minimize_comment "$target"; then
      continue
    fi
    for j in "${!pending_nodes[@]}"; do
      if [ "${pending_nodes[$j]}" = "$target" ]; then
        body=$(printf '%s' "${pending_bodies[$j]}" | base64 --decode 2>/dev/null || true)
        collapse_comment "${pending_ids[$j]}" "$body" || true
        break
      fi
    done
  done <<< "$(not_yet_minimized "${pending_nodes[@]}")"
}

main() {
  local login prior ids=() node_ids=() bodies=() id node_id body_b64

  login=$(resolve_bot_login)
  prior=$(prior_comments "$login" || true)

  while IFS=$'\t' read -r id node_id body_b64; do
    [ -n "$id" ] || continue
    ids+=("$id")
    node_ids+=("$node_id")
    bodies+=("$body_b64")
  done <<< "$prior"

  # A cap note says "this commit was not reviewed" -- it is not a newer report,
  # so it must not hide the last real one, in either mode. Nothing older is
  # reconciled. Consecutive capped pushes edit one note in place rather than
  # stacking a note per push; the next real review (a forced one) posts
  # normally and reconciles the note away along with everything before it.
  if [ "${KIND:-}" = "cap" ]; then
    if [ ${#ids[@]} -gt 0 ]; then
      local newest=$(( ${#ids[@]} - 1 )) newest_body
      newest_body=$(printf '%s' "${bodies[$newest]}" | base64 --decode 2>/dev/null || true)
      if comment_header "$newest_body" | grep -qxF "$CAP_MARKER"; then
        patch_comment "${ids[$newest]}"
        return
      fi
    fi
    post_comment
    return
  fi

  if [ "$MODE" = "update" ]; then
    if [ ${#ids[@]} -gt 0 ]; then
      # Prior comments are oldest-first (see prior_comments) -- the current
      # report is the LAST one, not the first. Picking ids[0] here edited the
      # oldest comment forever once append-mode history existed on the PR: the
      # edit landed on a comment nobody was looking at, and every real report
      # after it was left sitting there unminimized.
      local last=$(( ${#ids[@]} - 1 ))
      patch_comment "${ids[$last]}"
      # A PR that switched from append mode to update mode still has whatever
      # append-mode history it accumulated. Reconcile it the same way append
      # mode would, so the switch actually yields one current visible report
      # rather than one edited comment plus a trail of untouched ones.
      if [ "$last" -gt 0 ]; then
        local blob="" k
        for ((k = 0; k < last; k++)); do
          blob+="${ids[$k]}"$'\t'"${node_ids[$k]}"$'\t'"${bodies[$k]}"$'\n'
        done
        reconcile_older <<< "$blob"
      fi
    else
      post_comment
    fi
    return
  fi

  # Post the new report BEFORE touching the old ones. If minimizing is what
  # fails, the PR is left with two visible reports; if posting is what fails
  # after a minimize, the PR is left with none visible at all.
  post_comment
  reconcile_older <<< "$prior"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi

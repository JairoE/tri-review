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
# Optional env: RUN_URL COMMIT_SHA
set -euo pipefail

MODE="${MODE:-append}"
MARKER="${MARKER:-<!-- tri-review-report -->}"
SUPERSEDED_MARKER='<!-- tri-review-superseded -->'

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

# Our prior report comments, oldest first, as `id<TAB>node_id`.
prior_comments() {
  local login="$1"
  gh api "repos/$REPO/issues/$PR_NUMBER/comments" --paginate --jq \
    ".[] | select(.user.login == \"$login\") | select(.body | startswith(\"$MARKER\")) | [.id, .node_id] | @tsv"
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
not_yet_minimized() {
  local args=() id
  for id in "$@"; do args+=(-f "ids[]=$id"); done
  [ ${#args[@]} -eq 0 ] && return 0
  gh api graphql "${args[@]}" -f query='
    query($ids: [ID!]!) {
      nodes(ids: $ids) { ... on IssueComment { id isMinimized } }
    }' --jq '.data.nodes[] | select(.isMinimized == false) | .id' 2>/dev/null || true
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
# permission we are already known to have.
collapse_comment() {
  local id="$1" body superseded
  body=$(gh api "repos/$REPO/issues/comments/$id" --jq '.body' 2>/dev/null) || return 1
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

main() {
  local login prior ids=() node_ids=() line id node_id
  login=$(resolve_bot_login)
  prior=$(prior_comments "$login" || true)

  while IFS=$'\t' read -r id node_id; do
    [ -n "$id" ] || continue
    ids+=("$id")
    node_ids+=("$node_id")
  done <<< "$prior"

  if [ "$MODE" = "update" ]; then
    if [ ${#ids[@]} -gt 0 ]; then
      patch_comment "${ids[0]}"
    else
      post_comment
    fi
    return
  fi

  # Post the new report BEFORE touching the old ones. If minimizing is what
  # fails, the PR is left with two visible reports; if posting is what fails
  # after a minimize, the PR is left with none visible at all.
  post_comment

  [ ${#node_ids[@]} -eq 0 ] && return 0
  local target
  while read -r target; do
    [ -n "$target" ] || continue
    if ! minimize_comment "$target"; then
      # Map the node id back to the REST id for the PATCH fallback.
      local i=0
      while [ $i -lt ${#node_ids[@]} ]; do
        if [ "${node_ids[$i]}" = "$target" ]; then
          collapse_comment "${ids[$i]}" || true
          break
        fi
        i=$((i + 1))
      done
    fi
  done <<< "$(not_yet_minimized "${node_ids[@]}")"
}

main "$@"

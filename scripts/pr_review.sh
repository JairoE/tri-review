#!/usr/bin/env bash
# Post tri-review's applyable fixes to a PR as inline suggested changes (R9).
#
# REVIEW_FILE is what `tri-review --review-output` wrote: the exact body for
# POST /repos/{owner}/{repo}/pulls/{n}/reviews, one comment per applyable fix,
# each body starting with `<!-- tri-review-suggestion:<fingerprint> -->`.
#
# Driven entirely by environment variables so it can run against a stub `gh` on
# PATH -- see tests/test_pr_review.py.
#
# Required env: GH_TOKEN REPO PR_NUMBER REVIEW_FILE
# Optional env: GITHUB_OUTPUT
#
# This step never decides the run's result: every failure (a 403 on a fork PR,
# a 422, the network) is a ::warning:: and the script exits 0. So no `-e`;
# every call that can fail is checked where it is made.
set -uo pipefail

# resolve_bot_login lives in pr_comment.sh, which turns on -e for itself.
# shellcheck source=pr_comment.sh
source "$(dirname "${BASH_SOURCE[0]}")/pr_comment.sh"
set +e

# The fingerprint of a comment body, or nothing. Anchored at the very start of
# the body (\A, not ^, which matches at any line): a suggestion's own text can
# quote the marker -- when the diff under review is this repository -- and
# nothing after the first line may be read as ours.
JQ_FP='def fp: ((.body // "") | capture("\\A<!-- tri-review-suggestion:(?<fp>[0-9a-f]{16}) -->") | .fp) // empty;'

# A workflow command is one line. Escape the message the way GitHub's own
# toolkit does, so an error body from the API cannot end it early and start a
# command of its own on the next line.
warn() {
  local msg="$*"
  msg=${msg:0:500}
  msg=${msg//%/%25}
  msg=${msg//$'\r'/%0D}
  msg=${msg//$'\n'/%0A}
  printf '::warning::%s\n' "$msg"
}

set_output() {
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    echo "suggestions-posted=$1" >> "$GITHUB_OUTPUT"
  fi
  return 0
}

# Every review comment on the PR, as one JSON array.
#
# `--paginate` without `--jq` prints the pages as JSON arrays back to back; jq
# slurps them. Only stdout is JSON: gh can print a notice on stderr and still
# succeed, so stderr is kept apart and printed only when the call fails.
list_comments() {
  local pages err errfile
  errfile=$(mktemp)
  if ! pages=$(gh api "repos/$REPO/pulls/$PR_NUMBER/comments" --paginate 2>"$errfile"); then
    err=$(cat "$errfile")
    rm -f "$errfile"
    printf '%s' "${err:-$pages}"
    return 1
  fi
  rm -f "$errfile"
  printf '%s' "$pages" | jq -c -s '[.[][]]'
}

# Fingerprints already on the PR, as a JSON array -- from review comments by
# our own posting identity only. Anyone who can comment on the PR could
# otherwise write a marker with a fix's fingerprint and suppress that suggestion.
# The login goes in as a jq variable, not spliced into a filter.
posted_fingerprints() {
  printf '%s' "$2" | jq -c --arg login "$1" \
    "$JQ_FP"' [.[] | select(.user.login == $login) | fp] | unique'
}

# Other bot accounts that already posted one of the suggestions we are about to
# post, comma-separated. A GitHub App token cannot report its own login, so
# resolve_bot_login falls back to github-actions[bot] and the App's earlier
# suggestions are not counted as ours (as with review_cap.sh's
# warn_if_posted_as_someone_else). They are still not counted -- that would
# widen the guard above -- but the run says why they were posted again.
# Human accounts are never named: their markers are just noise.
others_with_our_fingerprints() {
  printf '%s' "$2" | jq -r --arg login "$1" --argjson ours "$3" \
    "$JQ_FP"' [.[] | select(.user.type == "Bot" and .user.login != $login)
      | select([fp] | any(. as $f | $ours | index($f))) | .user.login]
      | unique | join(",")'
}

# POST a JSON body read from stdin. Prints gh's output (on failure, the error
# body and the `gh: ... (HTTP nnn)` line); the exit status is gh's.
post_json() {
  gh api "$1" -X POST --input - 2>&1
}

# On 422 (one comment GitHub will not anchor fails the whole review), post each
# comment alone so one bad anchor costs one suggestion, not all of them.
# Sets POSTED to how many were posted. Not captured with $(...): its warnings
# must reach stdout, which is where the runner reads workflow commands.
POSTED=0
post_one_by_one() {
  local remaining="$1" comment out where
  while IFS= read -r comment; do
    [ -n "$comment" ] || continue
    if out=$(printf '%s' "$comment" | post_json "repos/$REPO/pulls/$PR_NUMBER/comments"); then
      POSTED=$((POSTED + 1))
    else
      where=$(printf '%s' "$comment" | jq -r '"\(.path):\(.line)"' 2>/dev/null)
      warn "Could not post the inline suggestion on ${where:-a comment}: $out"
    fi
  done < <(printf '%s' "$remaining" | jq -c '.commit_id as $c | .comments[] | . + {commit_id: $c}')
}

main() {
  local login comments posted remaining count out var ours others

  # Checked here rather than left to `set -u`, which would exit non-zero.
  for var in REPO PR_NUMBER REVIEW_FILE; do
    if [ -z "${!var:-}" ]; then
      warn "$var is not set; no inline suggestions posted."
      set_output 0
      return 0
    fi
  done

  if ! jq -e '.comments | type == "array"' "$REVIEW_FILE" >/dev/null 2>&1; then
    warn "No readable review payload at ${REVIEW_FILE}; no inline suggestions posted."
    set_output 0
    return 0
  fi

  login=$(resolve_bot_login)

  # Without the listing, a re-run would post every suggestion again; posting
  # nothing is the safer side of that.
  if ! comments=$(list_comments) || ! posted=$(posted_fingerprints "$login" "$comments"); then
    warn "Could not list this PR's review comments, so inline suggestions were not posted (to avoid duplicates): ${comments:-}"
    set_output 0
    return 0
  fi

  remaining=$(jq -c --argjson posted "$posted" \
    "$JQ_FP"' .comments |= map(select([fp] | all(. as $f | $posted | any(. == $f) | not)))' \
    "$REVIEW_FILE")
  count=$(printf '%s' "$remaining" | jq '.comments | length')

  if [ "$count" -eq 0 ]; then
    echo "All suggestions already posted."
    set_output 0
    return 0
  fi

  ours=$(printf '%s' "$remaining" | jq -c "$JQ_FP"' [.comments[] | fp]')
  others=$(others_with_our_fingerprints "$login" "$comments" "$ours" 2>/dev/null)
  if [ -n "$others" ]; then
    warn "Some of these inline suggestions were already posted by $others, but this run counts only those posted by $login, so they are posted again. A GitHub App token cannot report its own login; see the README's note on github-token."
  fi

  if out=$(printf '%s' "$remaining" | post_json "repos/$REPO/pulls/$PR_NUMBER/reviews"); then
    echo "Posted $count inline suggestion(s)."
    set_output "$count"
    return 0
  fi

  case "$out" in
    *"(HTTP 422)"*)
      warn "GitHub rejected the review as a whole (HTTP 422); posting each suggestion on its own."
      post_one_by_one "$remaining"
      echo "Posted $POSTED of $count inline suggestion(s) one by one."
      set_output "$POSTED"
      ;;
    *"(HTTP 403)"*)
      warn "GitHub refused the inline suggestions (HTTP 403). On a pull request from a fork the token is read-only, so suggestions cannot be posted; the report comment is unaffected. $out"
      set_output 0
      ;;
    *)
      warn "Could not post the inline suggestions: $out"
      set_output 0
      ;;
  esac
  return 0
}

main "$@"
exit 0

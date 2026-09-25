#!/usr/bin/env bash
# Decide whether this run is over the PR's review cap (the `max-reviews` input).
#
# Runs as the Action's first step, before Python or tri-review is installed, so
# a capped run costs one comment listing and nothing else. It lives in the
# Action rather than in each consumer's workflow because the Action owns what
# it counts: the comment markers and how comments are posted. A consumer that
# counts marker strings itself silently counts zero the day either changes.
#
# What counts is a run that produced a report -- the thing a review cap is
# buying. "Nothing to review" skips (path, empty-diff, triage) and failure
# comments do not count, and neither does a cap note itself:
#
# - Comments this Action posts from v1.1.0 on carry a running tally in their
#   header, `<!-- tri-review-reviews:N -->`: the number of reports on the PR up
#   to and including that comment. The count is the highest tally found. A
#   tally rather than a count of comments, because `comment-mode: update` edits
#   one comment in place forever -- counting comments would read 1 on every run
#   and the cap would never fire.
# - Older comments carry only the report marker. They are counted one each,
#   except those whose first line after the header is a skip or failure
#   headline, which v1.0.x wrote verbatim.
#
# Required env: GH_TOKEN REPO GITHUB_OUTPUT
# Optional env: PR_NUMBER MAX_REVIEWS FORCE MARKER
#
# Writes `prior-reviews` (empty when it could not be counted) and `capped` to
# GITHUB_OUTPUT; a capped run also gets `skipped`, `skip-reason=cap` and
# `exit-code=0`, standing in for the tri-review step that will not run.
set -euo pipefail

# shellcheck source=pr_comment.sh
source "$(dirname "${BASH_SOURCE[0]}")/pr_comment.sh"

# Reads prior_comments() rows on stdin; prints how many reports they record.
count_reviews() {
  local id node_id body_b64 body header tally max_tally=0 legacy=0
  while IFS=$'\t' read -r id node_id body_b64; do
    [ -n "$id" ] || continue
    body=$(printf '%s' "$body_b64" | base64 --decode 2>/dev/null || true)
    header=$(comment_header "$body")
    tally=$(printf '%s\n' "$header" | sed -n 's/^<!-- tri-review-reviews:\([0-9][0-9]*\) -->$/\1/p' | head -n 1)
    if [ -n "$tally" ]; then
      tally=$((10#$tally))
      [ "$tally" -gt "$max_tally" ] && max_tally=$tally
      continue
    fi
    case "$header" in
      *"content:**Nothing to review.**"* | *"content:**tri-review failed to produce a report**"*) ;;
      *content:*) legacy=$((legacy + 1)) ;;
    esac
  done
  echo $(( max_tally > legacy ? max_tally : legacy ))
}

output() { echo "$1" >> "$GITHUB_OUTPUT"; }

cap_main() {
  local max="${MAX_REVIEWS:-}" force="${FORCE:-}" prior="" login rows

  max="${max//[[:space:]]/}"
  if [ -n "$max" ] && ! [[ "$max" =~ ^[0-9]+$ ]]; then
    echo "::error::max-reviews must be a whole number, or empty for no cap; got '${MAX_REVIEWS}'."
    exit 1
  fi
  # A misspelled force ("yes", "True ") silently meaning false would cap the
  # very run someone asked to force; say so instead.
  case "$force" in
    "" | false) force=false ;;
    true) ;;
    *)
      echo "::error::force must be 'true' or 'false'; got '${FORCE}'."
      exit 1
      ;;
  esac

  # Counted even with no cap set: the comment step writes the tally from this,
  # so a consumer who turns max-reviews on later starts from the right number.
  if [ -n "${PR_NUMBER:-}" ]; then
    login=$(resolve_bot_login)
    if rows=$(prior_comments "$login"); then
      prior=$(count_reviews <<< "$rows")
    elif [ -n "$max" ]; then
      # Fail open, loudly. A transient API error should not leave a commit
      # unreviewed; the cost is one review, and the warning is on the run.
      echo "::warning::Could not list PR #$PR_NUMBER's comments to count earlier tri-review reviews, so max-reviews ($max) is not enforced on this run."
    fi
  elif [ -n "$max" ]; then
    echo "::warning::No PR number for this run, so max-reviews ($max) cannot be enforced."
  fi
  output "prior-reviews=$prior"

  if [ -n "$max" ] && [ -n "$prior" ] && [ "$prior" -ge "$max" ]; then
    if [ "$force" = "true" ]; then
      echo "::notice::PR #$PR_NUMBER has had $prior of $max tri-review reviews; reviewing anyway because force is set."
    else
      echo "::notice::PR #$PR_NUMBER has had $prior of $max tri-review reviews; skipping this run without calling any model. Set force to review it anyway."
      output "capped=true"
      output "skipped=true"
      output "skip-reason=cap"
      output "exit-code=0"
      return
    fi
  fi
  output "capped=false"
}

cap_main "$@"

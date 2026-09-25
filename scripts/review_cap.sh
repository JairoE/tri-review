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
#   to and including that comment (written by pr_comment.sh). A tally rather
#   than a count of comments, because `comment-mode: update` edits one comment
#   in place forever -- counting comments would read 1 on every run and the
#   cap would never fire.
# - Older comments carry only the report marker. They are counted one each,
#   except those whose first line after the header is a skip or failure
#   headline, which v1.0.x wrote verbatim.
#
# count_reviews, which does the counting, lives in pr_comment.sh so the tally
# written and the tally read can never disagree.
#
# Required env: GH_TOKEN REPO GITHUB_OUTPUT
# Optional env: PR_NUMBER MAX_REVIEWS FORCE MARKER POST_COMMENT
#
# Writes `prior-reviews` (empty when it could not be counted) and `capped` to
# GITHUB_OUTPUT; a capped run also gets `skipped`, `skip-reason=cap` and
# `exit-code=0`, standing in for the tri-review step that will not run.
set -euo pipefail

# shellcheck source=pr_comment.sh
source "$(dirname "${BASH_SOURCE[0]}")/pr_comment.sh"

# Only comments posted by our own login are counted -- anyone can write the
# markers in a comment of their own. But `gh api user` cannot name the login of
# a GitHub App installation token, and the fallback, github-actions[bot], is
# then not who posted our reports: the count reads zero on every run. Say so
# when another bot has tri-review reports on this PR and we found none.
warn_if_posted_as_someone_else() {
  local others
  others=$(gh api "repos/$REPO/issues/$PR_NUMBER/comments" --paginate --jq \
    ".[] | select(.user.type == \"Bot\") | select(.user.login != \"$1\") | select(.body | startswith(\"$MARKER\")) | .user.login" \
    2>/dev/null | sort -u | paste -sd, - || true)
  if [ -n "$others" ]; then
    echo "::warning::This PR has tri-review comments posted by $others, but this run counts only those posted by $1, so max-reviews sees none of them. A GitHub App token cannot report its own login; see the README's note on github-token."
  fi
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

  # The count comes from the comments this Action posts. With none posted, it
  # reads zero forever -- not an error anyone would otherwise notice.
  if [ -n "$max" ] && [ "${POST_COMMENT:-true}" != "true" ]; then
    echo "::warning::max-reviews ($max) counts the review comments this Action posts, and post-comment is '${POST_COMMENT}', so nothing is posted and the cap can never be reached."
  fi

  # Counted even with no cap set, for the prior-reviews output.
  if [ -n "${PR_NUMBER:-}" ]; then
    login=$(resolve_bot_login)
    if rows=$(prior_comments "$login"); then
      prior=$(count_reviews <<< "$rows")
      if [ -n "$max" ] && [ "$prior" = "0" ]; then warn_if_posted_as_someone_else "$login"; fi
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

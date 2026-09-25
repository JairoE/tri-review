"""The `max-reviews` / `force` cap, driven against a stub `gh`.

Two layers. The first runs scripts/review_cap.sh against hand-built comment
histories, including the shapes v1.0.x left on PRs that are still open. The
second runs a simulated PR end to end: the cap step and the "Post PR comment"
step are both executed exactly as action.yml has them -- the inline shell is
lifted out of the YAML, not copied -- against a stub `gh` that keeps a real
comment store, so the count one run reads is the tally the previous run wrote.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parent.parent
CAP_SCRIPT = ROOT / "scripts" / "review_cap.sh"
MARKER = "<!-- tri-review-report -->"
SUPERSEDED_MARKER = "<!-- tri-review-superseded -->"
CAP_MARKER = "<!-- tri-review-cap -->"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="the comment scripts shell out to jq"
)

# A stub that keeps state: STUB_STORE is a JSON list of comments, oldest first.
# The listing answers with what prior_comments() would extract from the real
# `gh api --paginate --jq` call (`id<TAB>node_id<TAB>base64(body)`), so posting
# and editing through it feed straight back into the next run's count.
_STUB = '''#!/usr/bin/env python3
import base64, json, os, sys
argv = sys.argv[1:]
joined = " ".join(argv)
store_path = os.environ["STUB_STORE"]
with open(store_path) as f:
    store = json.load(f)
with open(os.environ["GH_LOG"], "a") as log:
    log.write(json.dumps(argv) + "\\n")

if argv[:2] == ["api", "user"]:
    sys.exit(1)  # the default GITHUB_TOKEN is a bot, not a user

if "--paginate" in argv and "user.type" in joined:
    # warn_if_posted_as_someone_else: marker comments by other bot logins.
    print(os.environ.get("STUB_OTHER_BOTS", ""), end="")
    sys.exit(0)

if "--paginate" in argv:
    if os.environ.get("STUB_LIST_FAILS") == "1":
        print('{"message":"Resource not accessible by integration"}')
        sys.exit(1)
    for c in store:
        body = base64.b64encode(c["body"].encode()).decode()
        print(f"{c['id']}\\t{c['node_id']}\\t{body}")
    sys.exit(0)

if "graphql" in argv:
    if "minimizeComment" in joined:
        target = next(a.split("=", 1)[1] for a in argv if a.startswith("id="))
        for c in store:
            if c["node_id"] == target:
                c["minimized"] = True
    else:
        ids = [a.split("ids[]=", 1)[1] for a in argv if a.startswith("ids[]=")]
        for c in store:
            if c["node_id"] in ids and not c.get("minimized"):
                print(c["node_id"])
    with open(store_path, "w") as f:
        json.dump(store, f)
    sys.exit(0)

if "-X" in argv:
    body = json.loads(sys.stdin.read())["body"]
    if "POST" in argv:
        n = len(store) + 1
        store.append({"id": 100 + n, "node_id": f"NODE_{n}", "body": body})
    else:  # PATCH repos/.../issues/comments/<id>
        target = int(argv[1].rsplit("/", 1)[1])
        for c in store:
            if c["id"] == target:
                c["body"] = body
    with open(store_path, "w") as f:
        json.dump(store, f)
    sys.exit(0)

sys.exit(0)
'''


class FakePR:
    """A PR's comment thread, plus a stub `gh` that reads and writes it."""

    def __init__(self, tmp_path: Path, comments: list[str] | None = None):
        self.dir = tmp_path
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "gh"
        stub.write_text(_STUB)
        stub.chmod(0o755)
        self.store = tmp_path / "store.json"
        self.log = tmp_path / "gh.log"
        self.env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "STUB_STORE": str(self.store),
            "GH_LOG": str(self.log),
            "GH_TOKEN": "stub",
            "REPO": "octocat/Hello-World",
            "PR_NUMBER": "42",
            "MARKER": MARKER,
        }
        self.store.write_text(
            json.dumps(
                [
                    {"id": 100 + i, "node_id": f"NODE_{i}", "body": body}
                    for i, body in enumerate(comments or [], start=1)
                ]
            )
        )

    @property
    def comments(self) -> list[dict]:
        return json.loads(self.store.read_text())

    def cap(self, max_reviews="", force="", **env):
        """Run review_cap.sh; return (result, outputs dict)."""
        out = self.dir / "cap-output"
        out.write_text("")
        result = subprocess.run(
            ["bash", str(CAP_SCRIPT)],
            env={
                **self.env,
                "GITHUB_OUTPUT": str(out),
                "MAX_REVIEWS": max_reviews,
                "FORCE": force,
                **env,
            },
            capture_output=True,
            text=True,
        )
        outputs = dict(
            line.split("=", 1) for line in out.read_text().splitlines() if "=" in line
        )
        return result, outputs


def _legacy_report(text="## Consensus Findings\n\nreal findings"):
    return f"{MARKER}\n\n_Posted by [workflow run](u) reviewing commit `aaa`._\n\n{text}\n"


def _legacy_skip(reason_line="**Nothing to review.** Every file this PR changes is documentation."):
    return f"{MARKER}\n\n_Posted by [workflow run](u) reviewing commit `aaa`._\n\n{reason_line}\n"


def _folded(body):
    """What collapse_comment in pr_comment.sh turns an older comment into."""
    rest = body.split("\n", 1)[1]
    return (
        f"{MARKER}\n{SUPERSEDED_MARKER}\n\n"
        "<details><summary>Superseded by the review of `bbb`. Click to expand the earlier report.</summary>\n\n"
        f"{rest}\n</details>\n"
    )


def _tallied(n, text="## Consensus Findings", extra=""):
    extra_line = f"{extra}\n" if extra else ""
    return f"{MARKER}\n{extra_line}<!-- tri-review-reviews:{n} -->\n\n_Posted by x._\n\n{text}\n"


# --- review_cap.sh against fixed histories ---------------------------------


def test_no_cap_set_still_counts_but_never_caps(tmp_path):
    pr = FakePR(tmp_path, [_legacy_report(), _legacy_report()])
    result, out = pr.cap()

    assert result.returncode == 0, result.stderr
    assert out == {"prior-reviews": "2", "capped": "false"}


def test_below_the_cap_runs(tmp_path):
    pr = FakePR(tmp_path, [_legacy_report()])
    _, out = pr.cap(max_reviews="2")

    assert out["capped"] == "false"


def test_at_the_cap_skips_as_a_pass(tmp_path):
    pr = FakePR(tmp_path, [_legacy_report(), _legacy_report()])
    result, out = pr.cap(max_reviews="2")

    assert result.returncode == 0, result.stderr
    assert out == {
        "prior-reviews": "2",
        "capped": "true",
        "skipped": "true",
        "skip-reason": "cap",
        "exit-code": "0",
    }


def test_force_overrides_the_cap(tmp_path):
    pr = FakePR(tmp_path, [_legacy_report(), _legacy_report()])
    result, out = pr.cap(max_reviews="2", force="true")

    assert result.returncode == 0, result.stderr
    assert out["capped"] == "false"
    assert "skip-reason" not in out
    assert "force" in result.stdout


def test_force_false_is_the_same_as_unset(tmp_path):
    pr = FakePR(tmp_path, [_legacy_report(), _legacy_report()])
    _, out = pr.cap(max_reviews="2", force="false")

    assert out["capped"] == "true"


def test_zero_caps_every_run(tmp_path):
    pr = FakePR(tmp_path, [])
    _, out = pr.cap(max_reviews="0")

    assert out["capped"] == "true"


def test_legacy_skip_and_failure_comments_do_not_count(tmp_path):
    """PRs already open when this shipped hold v1.0.x comments that carry only
    the report marker. Reports count; skips and failures -- recognised by the
    headline v1.0.x wrote verbatim -- do not."""
    pr = FakePR(
        tmp_path,
        [
            _legacy_skip(),
            _legacy_skip("**Nothing to review.** A model read the diff and judged that it changes no behaviour."),
            _legacy_report(),
            _legacy_skip("**tri-review failed to produce a report** (exit code 2)."),
            _legacy_report(),
        ],
    )
    _, out = pr.cap(max_reviews="3")

    assert out["prior-reviews"] == "2"
    assert out["capped"] == "false"


def test_folded_comments_are_classified_like_the_comment_they_fold(tmp_path):
    pr = FakePR(
        tmp_path,
        [_folded(_legacy_skip()), _folded(_legacy_report()), _folded(_tallied(3))],
    )
    _, out = pr.cap()

    assert out["prior-reviews"] == "3"


def test_the_highest_tally_wins_over_the_number_of_comments(tmp_path):
    """comment-mode: update edits one comment in place forever, so counting
    comments would read 1 on every run and the cap would never fire."""
    pr = FakePR(tmp_path, [_tallied(5)])
    _, out = pr.cap(max_reviews="5")

    assert out["prior-reviews"] == "5"
    assert out["capped"] == "true"


def test_legacy_reports_still_count_when_newer_comments_carry_a_tally(tmp_path):
    """The first tallied comment on a PR with v1.0.x history was counted from
    that history, so it is never below it -- but taking the max also covers a
    tally written by a run that could not list comments."""
    pr = FakePR(tmp_path, [_legacy_report(), _legacy_report(), _legacy_report(), _tallied(1)])
    _, out = pr.cap()

    assert out["prior-reviews"] == "3"


def test_markers_quoted_inside_a_report_are_not_read_as_ours(tmp_path):
    """A report can quote anything from the diff it reviewed -- including these
    markers, when the diff is this repository. Only the header counts."""
    quoting = _legacy_report(
        "## Findings\n\n<!-- tri-review-reviews:99 -->\n<!-- tri-review-cap -->\n"
    )
    pr = FakePR(tmp_path, [quoting])
    _, out = pr.cap()

    assert out["prior-reviews"] == "1"


def test_a_cap_note_does_not_count(tmp_path):
    pr = FakePR(
        tmp_path,
        [_tallied(1), _tallied(1, "**Review cap reached (1/1).**", extra=CAP_MARKER)],
    )
    _, out = pr.cap()

    assert out["prior-reviews"] == "1"


def test_a_listing_failure_fails_open_and_says_so(tmp_path):
    """A transient API error should not leave a commit unreviewed -- but it must
    not be silent either, or a broken count pays for every push unnoticed."""
    pr = FakePR(tmp_path, [_legacy_report(), _legacy_report()])
    result, out = pr.cap(max_reviews="1", STUB_LIST_FAILS="1")

    assert result.returncode == 0, result.stderr
    assert out == {"prior-reviews": "", "capped": "false"}
    assert "::warning::" in result.stdout


def test_a_report_with_no_tally_after_tallied_ones_still_counts(tmp_path):
    """A report posted while its run could not count carries no tally. Taking
    the highest tally alone would drop it for good, and the PR would get one
    more paid review than max-reviews allows."""
    pr = FakePR(tmp_path, [_tallied(1), _tallied(2), _legacy_report()])
    _, out = pr.cap(max_reviews="3")

    assert out["prior-reviews"] == "3"
    assert out["capped"] == "true"


def test_a_comment_edited_in_the_web_ui_keeps_its_tally(tmp_path):
    """GitHub returns a body edited in the browser with CRLF line endings."""
    pr = FakePR(tmp_path, [_tallied(5).replace("\n", "\r\n")])
    _, out = pr.cap(max_reviews="5")

    assert out["prior-reviews"] == "5"


def test_max_reviews_without_posting_comments_warns(tmp_path):
    """The count comes from posted comments; with none posted it is zero
    forever and the cap silently never fires."""
    pr = FakePR(tmp_path, [])
    result, _ = pr.cap(max_reviews="2", POST_COMMENT="false")

    assert "::warning::" in result.stdout and "post-comment" in result.stdout


def test_reports_posted_by_another_bot_login_are_warned_about(tmp_path):
    """A GitHub App token cannot report its own login, so the fallback login
    matches none of the reports it posted and the count reads zero."""
    pr = FakePR(tmp_path, [])
    result, out = pr.cap(max_reviews="2", STUB_OTHER_BOTS="my-review-app[bot]\n")

    assert out["prior-reviews"] == "0"
    assert "::warning::" in result.stdout and "my-review-app[bot]" in result.stdout


def test_no_other_bot_warning_on_a_fresh_pr(tmp_path):
    pr = FakePR(tmp_path, [])
    result, _ = pr.cap(max_reviews="2")

    assert "::warning::" not in result.stdout


def test_a_cap_note_without_a_tally_does_not_count(tmp_path):
    """A cap note goes untallied when the comment step's listing failed; it
    is still not a review."""
    untallied_note = f"{MARKER}\n{CAP_MARKER}\n\n_Posted by x._\n\n**Review cap reached (1/1).**\n"
    pr = FakePR(tmp_path, [_tallied(1), untallied_note])
    _, out = pr.cap()

    assert out["prior-reviews"] == "1"


@pytest.mark.parametrize("bad", ["two", "-1", "1.5"])
def test_a_malformed_max_reviews_fails_the_step(tmp_path, bad):
    pr = FakePR(tmp_path, [])
    result, _ = pr.cap(max_reviews=bad)

    assert result.returncode != 0
    assert "::error::" in result.stdout


def test_a_malformed_force_fails_the_step(tmp_path):
    """A misspelled force silently meaning false would cap the very run someone
    asked to force."""
    pr = FakePR(tmp_path, [])
    result, _ = pr.cap(max_reviews="1", force="yes")

    assert result.returncode != 0


# --- a simulated PR through action.yml's own steps -------------------------


def _action_steps():
    return {s["name"]: s for s in yaml.safe_load((ROOT / "action.yml").read_text())["runs"]["steps"]}


def _workspace(pr: FakePR, stray: str | None = None) -> Path:
    """A fresh checkout. `stray` is a report.md the consumer's repo commits."""
    work = pr.dir / "work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    if stray is not None:
        (work / "report.md").write_text(stray)
    return work


# Stands in for the CLI: STUB_OUTCOME is "report", "skip:<reason>", or "fail".
_STUB_TRI_REVIEW = """#!/usr/bin/env bash
out=""
while [ $# -gt 0 ]; do
  [ "$1" = --output ] && { out="$2"; shift; }
  shift
done
case "$STUB_OUTCOME" in
  report) printf '## Consensus Findings\\n\\nx\\n' > "$out" ;;
  skip:*) echo "tri-review-status: skipped:${STUB_OUTCOME#skip:}" ;;
  fail) echo "no API keys" >&2; exit 2 ;;
esac
"""


def _run_step(pr: FakePR, work: Path, outcome: str) -> dict:
    """Run the "Run tri-review" step's shell exactly as action.yml has it,
    against a stub `tri-review`; return its outputs."""
    step = _action_steps()["Run tri-review"]
    runner_temp = pr.dir / "runner-temp"
    runner_temp.mkdir(exist_ok=True)
    stub = pr.dir / "bin" / "tri-review"
    stub.write_text(_STUB_TRI_REVIEW)
    stub.chmod(0o755)
    out = pr.dir / "run-output"
    out.write_text("")
    env = {
        **pr.env,
        "GITHUB_OUTPUT": str(out),
        "STUB_OUTCOME": outcome,
        "REPORT": step["env"]["REPORT"].replace("${{ runner.temp }}", str(runner_temp)),
    }
    result = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", step["run"]], cwd=work, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    return dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)


def _comment_step(pr: FakePR, work: Path, env: dict):
    script = _action_steps()["Post PR comment"]["run"].replace(
        "${{ github.action_path }}", str(ROOT)
    )
    env = {
        **pr.env,
        "RUN_URL": "https://example.invalid/run/1",
        "BODY_FILE": "comment-body.md",
        **env,
    }
    result = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", script], cwd=work, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr


def _post_comment(pr: FakePR, cap_out: dict, *, report: str | None, skip_reason="", mode="append", max_reviews="", stray=None):
    """Run the "Post PR comment" step's shell exactly as action.yml has it.
    A report arrives the way the run step hands it over: as report-path."""
    work = _workspace(pr, stray)
    report_path = ""
    if report is not None:
        report_file = pr.dir / "runner-temp" / "tri-review" / "report.md"
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text(report)
        report_path = str(report_file)
    (work / "tri-review-output.txt").write_text(
        f"tri-review-status: skipped:{skip_reason}\n" if skip_reason else ""
    )
    skipped = "true" if report is None and (skip_reason or cap_out.get("skipped")) else ""
    _comment_step(
        pr,
        work,
        {
            "MODE": mode,
            "SKIPPED": skipped,
            "SKIP_REASON": skip_reason or cap_out.get("skip-reason", ""),
            "EXIT_CODE": "0",
            "PRIOR_REVIEWS": cap_out.get("prior-reviews", ""),
            "MAX_REVIEWS": max_reviews,
            "REPORT": report_path,
        },
    )


def _push(pr, *, max_reviews="2", force="", review=True, skip_reason="", mode="append"):
    """One workflow run: the cap step, then (unless capped) a report or a skip,
    then the comment step. Returns the cap step's outputs."""
    _, out = pr.cap(max_reviews=max_reviews, force=force)
    if out.get("capped") == "true":
        _post_comment(pr, out, report=None, mode=mode, max_reviews=max_reviews)
    elif review and not skip_reason:
        _post_comment(pr, out, report="## Consensus Findings\n\nx\n", mode=mode, max_reviews=max_reviews)
    else:
        _post_comment(pr, out, report=None, skip_reason=skip_reason, mode=mode, max_reviews=max_reviews)
    return out


def test_skips_do_not_use_up_the_cap_but_reviews_do(tmp_path):
    """The shape of a real PR: docs-only pushes first, then code."""
    pr = FakePR(tmp_path)

    assert _push(pr, skip_reason="path")["capped"] == "false"
    assert _push(pr, skip_reason="triage")["capped"] == "false"
    # Two skip comments are on the PR. A plain marker count would cap here.
    assert _push(pr)["prior-reviews"] == "0"
    assert _push(pr)["prior-reviews"] == "1"
    capped = _push(pr)
    assert capped["capped"] == "true" and capped["skip-reason"] == "cap"

    note = pr.comments[-1]["body"]
    assert CAP_MARKER in note and "Review cap reached (2/2)" in note


def test_a_cap_note_leaves_the_last_report_visible(tmp_path):
    pr = FakePR(tmp_path)
    _push(pr)
    _push(pr)
    _push(pr)  # capped

    report, note = pr.comments[-2], pr.comments[-1]
    assert CAP_MARKER in note["body"]
    assert not report.get("minimized"), "the cap note hid the last real report"


def test_consecutive_capped_pushes_edit_one_note(tmp_path):
    pr = FakePR(tmp_path)
    _push(pr)
    _push(pr)
    _push(pr)
    _push(pr)
    _push(pr)

    notes = [c for c in pr.comments if CAP_MARKER in c["body"]]
    assert len(notes) == 1
    assert len(pr.comments) == 3


def test_a_forced_review_runs_counts_and_hides_the_cap_note(tmp_path):
    pr = FakePR(tmp_path)
    _push(pr)
    _push(pr)
    _push(pr)  # capped

    forced = _push(pr, force="true")
    assert forced["capped"] == "false"
    assert "<!-- tri-review-reviews:3 -->" in pr.comments[-1]["body"]
    assert all(c.get("minimized") for c in pr.comments[:-1])
    # ...and the next unforced push is capped again, now at 3 of 2.
    again = _push(pr)
    assert again["capped"] == "true" and again["prior-reviews"] == "3"


def test_the_cap_fires_in_update_mode(tmp_path):
    """update mode keeps one comment; the tally it carries is the count."""
    pr = FakePR(tmp_path)
    _push(pr, mode="update")
    _push(pr, mode="update")
    assert len(pr.comments) == 1

    assert _push(pr, mode="update")["capped"] == "true"
    report = next(c for c in pr.comments if CAP_MARKER not in c["body"])
    assert "## Consensus Findings" in report["body"], "the cap note overwrote the last report"


@pytest.mark.parametrize("mode", ["append", "update"])
def test_a_failed_cap_step_listing_does_not_lose_the_tally(tmp_path, mode):
    """The comment step tallies from its own listing, so one failed listing in
    the cap step neither drops the tally (update mode would overwrite the only
    comment holding it) nor leaves an uncounted report behind."""
    pr = FakePR(tmp_path)
    _push(pr, max_reviews="3", mode=mode)
    _push(pr, max_reviews="3", mode=mode)

    _, out = pr.cap(max_reviews="3", STUB_LIST_FAILS="1")
    assert out["prior-reviews"] == ""
    _post_comment(pr, out, report="## Consensus Findings\n", mode=mode, max_reviews="3")

    assert "<!-- tri-review-reviews:3 -->" in pr.comments[-1]["body"]
    assert _push(pr, max_reviews="3", mode=mode)["capped"] == "true"


def test_a_stray_report_md_in_the_workspace_is_not_posted_on_a_capped_run(tmp_path):
    """tri-review never ran, so a report.md the consumer's repo happens to
    commit at its root is not this run's report -- posting it would present it
    as a review of this commit and count it toward the cap."""
    pr = FakePR(tmp_path, [_tallied(2)])
    _, out = pr.cap(max_reviews="2")
    _post_comment(pr, out, report=None, max_reviews="2", stray="# someone else's report.md\n")

    note = pr.comments[-1]["body"]
    assert CAP_MARKER in note and "someone else" not in note
    assert "<!-- tri-review-reviews:2 -->" in note


@pytest.mark.parametrize(
    "outcome, expect",
    [
        ("skip:path", "Every file this PR changes is documentation"),
        ("skip:empty-diff", "diff came back empty"),
        ("skip:triage", "judged that it changes no behaviour"),
        ("fail", "tri-review failed to produce a report"),
    ],
)
def test_a_stray_report_md_is_not_posted_when_tri_review_writes_none(tmp_path, outcome, expect):
    """The consumer's repo commits a report.md at its root. A run that skips or
    fails writes no report, and must say so -- not post that file as the review
    of this commit, and not count it toward the cap. Driven through the real
    run step, so report-path is whatever action.yml actually hands over."""
    pr = FakePR(tmp_path, [_tallied(1)])
    _, cap_out = pr.cap(max_reviews="3")
    work = _workspace(pr, stray="# someone else's report.md\n")

    run_out = _run_step(pr, work, outcome)
    assert "report-path" not in run_out
    assert (work / "report.md").read_text() == "# someone else's report.md\n", "the run step touched the checkout"

    _comment_step(
        pr,
        work,
        {
            "MODE": "append",
            "SKIPPED": run_out.get("skipped", ""),
            "SKIP_REASON": run_out.get("skip-reason", ""),
            "EXIT_CODE": run_out["exit-code"],
            "PRIOR_REVIEWS": cap_out["prior-reviews"],
            "MAX_REVIEWS": "3",
            "REPORT": run_out.get("report-path", ""),
        },
    )

    body = pr.comments[-1]["body"]
    assert "someone else" not in body
    assert expect in body
    assert "<!-- tri-review-reviews:1 -->" in body, "a skip or failure was counted as a review"


def test_a_real_report_is_written_outside_the_checkout_and_posted(tmp_path):
    pr = FakePR(tmp_path, [_tallied(1)])
    _, cap_out = pr.cap(max_reviews="3")
    work = _workspace(pr, stray="# someone else's report.md\n")

    run_out = _run_step(pr, work, "report")
    report_path = Path(run_out["report-path"])
    assert report_path.is_absolute() and work not in report_path.parents

    _comment_step(
        pr,
        work,
        {
            "MODE": "append",
            "SKIPPED": "",
            "SKIP_REASON": "",
            "EXIT_CODE": run_out["exit-code"],
            "PRIOR_REVIEWS": cap_out["prior-reviews"],
            "MAX_REVIEWS": "3",
            "REPORT": str(report_path),
        },
    )

    body = pr.comments[-1]["body"]
    assert "## Consensus Findings" in body and "someone else" not in body
    assert "<!-- tri-review-reviews:2 -->" in body


def test_legacy_history_carries_into_the_tally(tmp_path):
    pr = FakePR(tmp_path, [_legacy_skip(), _legacy_report()])

    assert _push(pr)["prior-reviews"] == "1"
    assert "<!-- tri-review-reviews:2 -->" in pr.comments[-1]["body"]
    assert _push(pr)["capped"] == "true"


# --- action.yml structure ---------------------------------------------------


def test_the_cap_step_runs_before_anything_is_installed():
    steps = yaml.safe_load((ROOT / "action.yml").read_text())["runs"]["steps"]
    names = [s["name"] for s in steps]

    assert names[0] == "Check review cap"
    for name in ("Set up Python", "Install tri-review", "Run tri-review"):
        step = next(s for s in steps if s["name"] == name)
        assert "steps.cap.outputs.capped != 'true'" in step.get("if", ""), name


def test_every_action_the_action_uses_is_pinned_to_a_commit():
    """Inherited by every consumer: a tag here is a supply chain they never
    opted into (see 8f31927)."""
    import re

    steps = yaml.safe_load((ROOT / "action.yml").read_text())["runs"]["steps"]
    uses = [s["uses"] for s in steps if "uses" in s]
    assert uses
    for ref in uses:
        assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", ref), ref


def test_capped_outputs_fall_back_to_the_cap_step():
    outputs = yaml.safe_load((ROOT / "action.yml").read_text())["outputs"]
    for name in ("skipped", "skip-reason", "exit-code"):
        assert "steps.cap.outputs." in outputs[name]["value"], name

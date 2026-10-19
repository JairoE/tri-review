"""Diff hunks and which fixes can become inline suggestions (R7)."""

from tri_review.schema import NormalizedFinding
from tri_review.suggestions import MAX_SUGGESTION_LINES, FileHunks, applyability, parse_hunks

# src/auth.py: one hunk covering new lines 40-46, another covering 100-149.
DIFF = """\
diff --git a/src/auth.py b/src/auth.py
index 1111111..2222222 100644
--- a/src/auth.py
+++ b/src/auth.py
@@ -40,6 +40,7 @@ def refresh(session):
 def refresh(session):
     token = fetch()
-    session.token = token
+    if session.active:
+        session.token = token
     return token


@@ -99,50 +100,50 @@ class Session:
""" + "".join(f" line {n}\n" for n in range(100, 150)) + """\
diff --git a/src/old.py b/src/old.py
deleted file mode 100644
index 3333333..0000000
--- a/src/old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def gone():
-    pass
"""


def make(**overrides) -> NormalizedFinding:
    base = dict(
        id="F1",
        kind="consensus",
        reported_by=["gpt-6-sol", "claude-sonnet-5"],
        severity="major",
        category="bug",
        file="src/auth.py",
        line=42,
        title="Token refresh races logout",
        problem="refresh() writes the token after logout() cleared it.",
        trigger="Logout while a refresh request is in flight.",
        fix="Check the session is still set before assigning.",
        fix_start_line=42,
        fix_end_line=43,
        replacement="    if session.active:\n        session.token = token\n",
        action="blocking",
        falsified_by="A lock around refresh and logout.",
    )
    base.update(overrides)
    return NormalizedFinding(**base)


HUNKS = parse_hunks(DIFF)


def reason(**overrides) -> str | None:
    return applyability(make(**overrides), HUNKS)


# --- parse_hunks ---------------------------------------------------------


def test_parses_new_side_ranges_per_file():
    assert HUNKS["src/auth.py"] == FileHunks(deleted=False, ranges=[(40, 46), (100, 149)])


def test_deleted_file_is_keyed_by_old_path_and_marked_deleted():
    assert HUNKS["src/old.py"].deleted is True
    assert HUNKS["src/old.py"].ranges == []


def test_hunk_with_zero_new_lines_has_no_range():
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -5,2 +4,0 @@\n-x = 1\n-y = 2\n"
    )
    assert parse_hunks(diff)["a.py"] == FileHunks(deleted=False, ranges=[])


def test_hunk_header_without_count_is_one_line():
    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -3 +3 @@\n-x = 1\n+x = 2\n"
    assert parse_hunks(diff)["a.py"].ranges == [(3, 3)]


def test_rename_is_keyed_by_new_path():
    diff = (
        "diff --git a/old/name.py b/new/name.py\n"
        "similarity index 90%\nrename from old/name.py\nrename to new/name.py\n"
        "--- a/old/name.py\n+++ b/new/name.py\n"
        "@@ -1,2 +1,2 @@\n-a = 1\n+a = 2\n b = 3\n"
    )
    hunks = parse_hunks(diff)
    assert list(hunks) == ["new/name.py"]
    assert hunks["new/name.py"].ranges == [(1, 2)]


def test_no_newline_at_end_of_file_marker_is_ignored():
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1,1 +1,1 @@\n-x = 1\n\\ No newline at end of file\n+x = 2\n"
        "\\ No newline at end of file\n"
        "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n"
        "@@ -1 +1,2 @@\n x\n+y\n"
    )
    hunks = parse_hunks(diff)
    assert hunks["a.py"].ranges == [(1, 1)]
    assert hunks["b.py"].ranges == [(1, 2)]


def test_body_lines_that_look_like_headers_are_body():
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1,2 +1,2 @@\n--- a/other.py\n+++ b/other.py\n x\n"
    )
    assert list(parse_hunks(diff)) == ["a.py"]


def test_truncated_diff_still_parses_complete_hunks():
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1,2 +1,2 @@\n-a\n+b\n c\n"
        "@@ -20,10 +20,10 @@\n x\n y\n"  # cut off mid-hunk
    )
    assert parse_hunks(diff)["a.py"].ranges == [(1, 2)]


def test_incomplete_hunk_does_not_swallow_the_next_file():
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -20,10 +20,10 @@\n x\n"  # cut off mid-hunk
        "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n"
        "@@ -1,2 +1,2 @@\n-a\n+b\n c\n"
    )
    hunks = parse_hunks(diff)
    assert hunks["a.py"].ranges == []
    assert hunks["b.py"].ranges == [(1, 2)]


def test_incomplete_hunk_does_not_swallow_the_next_hunk():
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1,5 +1,5 @@\n x\n"  # miscounted: ends after one line
        "@@ -30,2 +30,2 @@\n-a\n+b\n c\n"
    )
    assert parse_hunks(diff)["a.py"].ranges == [(30, 31)]


def test_deletion_without_old_path_is_not_keyed_by_empty_string():
    diff = "diff --git a/a.py b/a.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-a\n"
    assert "" not in parse_hunks(diff)


def test_empty_diff_has_no_files():
    assert parse_hunks("") == {}


# --- applyability: one passing and one failing case per R7 condition ------


def test_applyable_finding_has_no_reason():
    assert reason() is None


def test_condition_1_empty_replacement_is_an_applyable_deletion():
    assert reason(replacement="") is None


def test_condition_1_no_replacement():
    assert reason(replacement=None) == "no replacement code"


def test_condition_1_no_fix_start_line():
    assert reason(fix_start_line=None, fix_end_line=None) == "no replacement code"


def test_condition_2_file_in_diff_passes():
    assert reason(file="src/auth.py") is None


def test_condition_2_file_not_in_diff():
    assert reason(file="src/other.py") == "file not in this diff"


def test_condition_2_file_deleted():
    assert reason(file="src/old.py", fix_start_line=1, fix_end_line=1) == "file deleted in this PR"


def test_condition_3_range_on_hunk_edges_passes():
    assert reason(fix_start_line=40, fix_end_line=46) is None


def test_condition_3_lines_outside_the_diff():
    assert reason(fix_start_line=47, fix_end_line=47) == "lines outside the diff"


def test_condition_3_range_leaving_its_hunk_is_outside_the_diff():
    assert reason(fix_start_line=45, fix_end_line=50) == "lines outside the diff"


def test_condition_3_range_spans_more_than_one_hunk():
    assert reason(fix_start_line=44, fix_end_line=101) == "range spans more than one hunk"


def test_condition_4_forty_lines_passes():
    assert MAX_SUGGESTION_LINES == 40
    assert reason(fix_start_line=100, fix_end_line=139) is None


def test_condition_4_range_longer_than_40_lines():
    assert reason(fix_start_line=100, fix_end_line=140) == "range longer than 40 lines"


def test_condition_5_previously_rejected_with_new_evidence_passes():
    assert reason(previously_rejected="Intended.", new_evidence="The lock was removed.") is None


def test_condition_5_previously_rejected():
    assert reason(previously_rejected="Intended.") == "previously rejected"


def test_blank_new_evidence_is_not_evidence():
    assert reason(previously_rejected="Intended.", new_evidence="   ") == "previously rejected"


def test_blank_previously_rejected_is_not_a_rejection():
    assert reason(previously_rejected="   ") is None


def test_condition_6_one_reporter_passes():
    assert reason(reported_by=["gemini-3-pro"], kind="unique") is None


def test_condition_6_no_reviewer_reported_it():
    assert reason(reported_by=[], kind="synthesizer") == "no reviewer reported it"


def test_first_failing_condition_wins():
    assert (
        reason(file="src/other.py", previously_rejected="Intended.", reported_by=[])
        == "file not in this diff"
    )

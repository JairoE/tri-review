"""Path gates decide what a review is spent on, so their glob semantics matter."""

import pytest

from tri_review import gating


@pytest.mark.parametrize(
    "path, pattern, expected",
    [
        # `**/` spans zero directories as well as many -- the case plain fnmatch
        # gets wrong, and the one that decides whether a root-level README is
        # recognised as documentation at all.
        ("README.md", "**/*.md", True),
        ("docs/guide.md", "**/*.md", True),
        ("docs/a/b/c.md", "**/*.md", True),
        ("src/app.py", "**/*.md", False),
        ("a.mdx", "**/*.md", False),
        # A pattern with no separator matches the basename at any depth.
        ("uv.lock", "*.lock", True),
        ("sub/dir/uv.lock", "*.lock", True),
        # A pattern with a separator is anchored, and `*` stops at one.
        ("src/app.py", "src/*.py", True),
        ("src/pkg/app.py", "src/*.py", False),
        ("docs/img/x.png", "docs/**", True),
        ("docsite/x.png", "docs/**", False),
        # Regex metacharacters in a path are literal, not operators.
        ("a+b.py", "a+b.py", True),
        ("axb.py", "a+b.py", False),
    ],
)
def test_glob_semantics(path, pattern, expected):
    assert gating.matches_any(path, (pattern,)) is expected


def test_matches_any_is_a_union():
    assert gating.matches_any("go.sum", ("**/*.md", "go.sum"))
    assert not gating.matches_any("main.go", ("**/*.md", "go.sum"))


def test_partition_preserves_order_and_reports_both_sides():
    paths = ["README.md", "src/app.py", "uv.lock", "src/db.py"]
    reviewable, excluded = gating.partition(paths, ("**/*.md", "**/*.lock"))
    assert reviewable == ["src/app.py", "src/db.py"]
    assert excluded == ["README.md", "uv.lock"]


def test_partition_with_no_patterns_reviews_everything():
    reviewable, excluded = gating.partition(["README.md"], ())
    assert reviewable == ["README.md"]
    assert excluded == []


# --- filter_diff ------------------------------------------------------------


def _section(path, old=None, new=None, extra=""):
    old = f"a/{path}" if old is None else old
    new = f"b/{path}" if new is None else new
    return f"diff --git a/{path} b/{path}\n{extra}--- {old}\n+++ {new}\n@@ -1 +1 @@\n-x\n+y\n"


def test_filter_diff_drops_matches_at_every_depth_and_keeps_code():
    diff = "".join(
        _section(p) for p in ("README.md", "docs/a.md", "docs/x/y/z.md", "src/a.py")
    )
    kept = gating.filter_diff(diff, ("**/*.md",))
    assert kept == _section("src/a.py")


def test_filter_diff_with_no_patterns_is_the_identity():
    diff = _section("README.md")
    assert gating.filter_diff(diff, ()) == diff


def test_filter_diff_matches_a_deletion_on_its_old_path():
    deleted = _section("docs/gone.md", new="/dev/null", extra="deleted file mode 100644\n")
    assert gating.filter_diff(deleted + _section("src/a.py"), ("**/*.md",)) == _section("src/a.py")


def test_filter_diff_handles_sections_with_no_file_headers():
    binary = "diff --git a/img/logo.png b/img/logo.png\nBinary files differ\n"
    renamed = (
        "diff --git a/old.md b/new.md\nsimilarity index 100%\n"
        "rename from old.md\nrename to new.md\n"
    )
    code = _section("src/a.py")
    assert gating.filter_diff(binary + renamed + code, ("**/*.png", "**/*.md")) == code


def test_filter_diff_does_not_read_hunk_lines_as_headers():
    """A removed line starting `-- ` renders as `--- ` in the hunk; it must
    not be taken for the file's pre-image path."""
    section = "diff --git a/src/q.sql b/src/q.sql\n--- a/src/q.sql\n+++ b/src/q.sql\n@@ -1 +1 @@\n--- notes.md\n+x\n"
    assert gating.filter_diff(section, ("**/*.md",)) == section


def test_filter_diff_agrees_with_partition():
    """What the skip report says was dropped is exactly what the payload lost."""
    paths = ["README.md", "docs/app_state.md", "docs/superpowers/plans/p.md", "server/app.ts", "uv.lock"]
    patterns = ("**/*.md", "**/*.lock")
    reviewable, _ = gating.partition(paths, patterns)
    kept = gating.filter_diff("".join(_section(p) for p in paths), patterns)
    assert kept == "".join(_section(p) for p in reviewable)

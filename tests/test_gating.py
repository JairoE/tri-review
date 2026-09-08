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

import pytest

from tri_review import context as context_mod
from tri_review.context import build_context, github_reader, parse_changed_files
from tri_review.schema import ContextSummary

MODIFIED = """diff --git a/src/app.py b/src/app.py
index 111..222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,2 +1,2 @@
-old
+new
"""

ADDED = """diff --git a/new_file.py b/new_file.py
new file mode 100644
--- /dev/null
+++ b/new_file.py
@@ -0,0 +1 @@
+hello
"""

DELETED = """diff --git a/gone.py b/gone.py
deleted file mode 100644
--- a/gone.py
+++ /dev/null
@@ -1 +0,0 @@
-bye
"""

RENAMED = """diff --git a/old_name.py b/new_name.py
similarity index 90%
rename from old_name.py
rename to new_name.py
--- a/old_name.py
+++ b/new_name.py
@@ -1 +1 @@
-a
+b
"""


def test_parses_modified_file():
    assert parse_changed_files(MODIFIED) == ["src/app.py"]


def test_parses_added_file():
    assert parse_changed_files(ADDED) == ["new_file.py"]


def test_skips_deleted_file():
    assert parse_changed_files(DELETED) == []


def test_rename_uses_post_image_path():
    assert parse_changed_files(RENAMED) == ["new_name.py"]


def test_multiple_files_deduplicated():
    assert parse_changed_files(MODIFIED + ADDED + MODIFIED) == ["src/app.py", "new_file.py"]


def test_build_context_reads_changed_file(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('real contents')\n")
    ctx = build_context(MODIFIED, root=tmp_path)
    assert "print('real contents')" in ctx.files["src/app.py"]
    assert "print('real contents')" in ctx.render()
    assert ctx.dropped == []


def test_missing_file_is_recorded_not_fatal(tmp_path):
    ctx = build_context(MODIFIED, root=tmp_path)
    assert ctx.missing == ["src/app.py"]
    assert ctx.files == {}
    assert "diff --git" in ctx.render()


def test_over_budget_drops_largest_first(tmp_path):
    (tmp_path / "small.py").write_text("x" * 40)
    (tmp_path / "big.py").write_text("y" * 40_000)
    diff = (
        "diff --git a/small.py b/small.py\n--- a/small.py\n+++ b/small.py\n@@ -1 +1 @@\n+x\n"
        "diff --git a/big.py b/big.py\n--- a/big.py\n+++ b/big.py\n@@ -1 +1 @@\n+y\n"
    )
    ctx = build_context(diff, root=tmp_path, budget=200)
    assert "small.py" in ctx.files
    assert ctx.dropped == ["big.py"]
    assert "big.py" in ctx.render()  # named in the truncation note


def _diff_touching(path: str) -> str:
    return f"diff --git a/x b/x\n--- a/x\n+++ b/{path}\n@@ -1 +1 @@\n+x\n"


def test_traversal_path_is_refused_not_read(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("PRIVATE KEY")

    ctx = build_context(_diff_touching("../secret.txt"), root=root)

    assert ctx.files == {}
    assert ctx.rejected == ["../secret.txt"]
    assert "PRIVATE KEY" not in ctx.render()


def test_symlink_out_of_the_repo_is_refused(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("PRIVATE KEY")
    (root / "notes.md").symlink_to(tmp_path / "secret.txt")

    ctx = build_context(_diff_touching("notes.md"), root=root)

    assert ctx.rejected == ["notes.md"]
    assert "PRIVATE KEY" not in ctx.render()


def test_symlink_inside_the_repo_is_still_read(tmp_path):
    """Containment is the rule, not symlink-phobia -- in-repo links are fine."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "real.py").write_text("in-repo contents")
    (root / "alias.py").symlink_to(root / "real.py")

    ctx = build_context(_diff_touching("alias.py"), root=root)

    assert ctx.rejected == []
    assert ctx.files["alias.py"] == "in-repo contents"


def test_diff_alone_over_budget_is_reported(tmp_path):
    (tmp_path / "small.py").write_text("x" * 40)
    diff = _diff_touching("small.py") + "+" + "y" * 4_000

    ctx = build_context(diff, root=tmp_path, budget=100)

    assert ctx.diff_overflow > 0
    assert ctx.files == {}
    assert ctx.dropped == ["small.py"]


def test_diff_within_budget_reports_no_overflow(tmp_path):
    (tmp_path / "small.py").write_text("x" * 40)
    ctx = build_context(_diff_touching("small.py"), root=tmp_path, budget=100_000)
    assert ctx.diff_overflow == 0


def test_everything_fits_when_budget_is_ample(tmp_path):
    (tmp_path / "small.py").write_text("x" * 40)
    diff = "diff --git a/small.py b/small.py\n--- a/small.py\n+++ b/small.py\n@@ -1 +1 @@\n+x\n"
    ctx = build_context(diff, root=tmp_path, budget=100_000)
    assert ctx.dropped == []
    assert ctx.estimated_tokens > 0


# --- the pluggable reader ---------------------------------------------------


def test_a_custom_reader_supplies_file_contents():
    ctx = build_context(MODIFIED, reader=lambda rel: f"contents of {rel}")
    assert ctx.files["src/app.py"] == "contents of src/app.py"


def test_reader_returning_none_lands_in_missing():
    ctx = build_context(MODIFIED, reader=lambda rel: None)
    assert ctx.missing == ["src/app.py"]
    assert ctx.files == {}


def test_traversal_is_refused_before_the_reader_runs():
    """The guard is at the boundary, so a reader never sees an escaping path.

    Asserted by giving it a reader that fails the test if called at all -- the
    github reader would otherwise spend the user's credentials on the request.
    """

    def explode(rel):
        pytest.fail(f"reader was called with an escaping path: {rel!r}")

    ctx = build_context(_diff_touching("../../etc/passwd"), reader=explode)
    assert ctx.rejected == ["../../etc/passwd"]
    assert ctx.files == {}


def test_absolute_path_is_refused_before_the_reader_runs():
    def explode(rel):
        pytest.fail(f"reader was called with an absolute path: {rel!r}")

    ctx = build_context(_diff_touching("/etc/passwd"), reader=explode)
    assert ctx.rejected == ["/etc/passwd"]


def test_github_reader_reads_at_the_given_ref(monkeypatch):
    seen = {}

    def fake_fetch(repo, path, ref):
        seen["args"] = (repo, path, ref)
        return "remote contents"

    monkeypatch.setattr(context_mod.github, "fetch_file_content", fake_fetch)

    ctx = build_context(MODIFIED, reader=github_reader("octocat/Hello-World", "c" * 40))

    assert seen["args"] == ("octocat/Hello-World", "src/app.py", "c" * 40)
    assert ctx.files["src/app.py"] == "remote contents"


# --- preview_context --------------------------------------------------------


def test_preview_context_makes_no_model_calls(monkeypatch):
    """The confirm-before-spending step must be free."""
    monkeypatch.setattr(context_mod.github, "fetch_diff", lambda pr, repo=None, exclude=(): MODIFIED)
    monkeypatch.setattr(
        context_mod.github, "fetch_pr_meta", lambda pr, repo=None: {"head_sha": "d" * 40}
    )
    monkeypatch.setattr(context_mod.github, "fetch_file_content", lambda r, p, ref: "body")

    ctx = context_mod.preview_context("42", repo="octocat/Hello-World")

    assert ctx.files == {"src/app.py": "body"}


def test_preview_context_cwd_mode_needs_no_pr_metadata(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(context_mod.github, "fetch_diff", lambda pr, repo=None, exclude=(): MODIFIED)
    monkeypatch.setattr(
        context_mod.github,
        "fetch_pr_meta",
        lambda pr, repo=None: pytest.fail("cwd mode must not resolve a head SHA"),
    )

    ctx = context_mod.preview_context("42")

    assert ctx.missing == ["src/app.py"]  # nothing on disk in the temp cwd


# --- ContextSummary ---------------------------------------------------------


def test_context_summary_round_trips_a_real_context(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('x')\n")
    ctx = build_context(MODIFIED, root=tmp_path)

    summary = ContextSummary.of(ctx)

    assert summary.files_included == ["src/app.py"]
    assert summary.estimated_tokens == ctx.estimated_tokens
    assert summary.diff_lines == len(MODIFIED.splitlines())
    assert summary.dropped == [] and summary.rejected == []


def test_context_summary_carries_the_refusals(tmp_path):
    ctx = build_context(_diff_touching("../secret.txt"), root=tmp_path)
    assert ContextSummary.of(ctx).rejected == ["../secret.txt"]

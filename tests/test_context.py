from tri_review.context import build_context, parse_changed_files

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


def test_everything_fits_when_budget_is_ample(tmp_path):
    (tmp_path / "small.py").write_text("x" * 40)
    diff = "diff --git a/small.py b/small.py\n--- a/small.py\n+++ b/small.py\n@@ -1 +1 @@\n+x\n"
    ctx = build_context(diff, root=tmp_path, budget=100_000)
    assert ctx.dropped == []
    assert ctx.estimated_tokens > 0

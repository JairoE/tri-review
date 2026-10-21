"""Per-model Markdown rendering — the side-by-side view's unit of content."""

from tri_review.schema import Finding, ReviewResult, render_findings_md


def _finding(**overrides):
    base = dict(
        file="src/app.py",
        line=12,
        severity="major",
        category="bug",
        title="Off-by-one in the loop bound",
        detail="The range excludes the final element.",
        trigger="Calling it with a one-element list.",
    )
    base.update(overrides)
    return Finding(**base)


def test_renders_a_heading_and_location_per_finding():
    md = render_findings_md(ReviewResult(model="m1", findings=[_finding()]))
    assert "Off-by-one in the loop bound" in md
    assert "`src/app.py:12`" in md
    assert "The range excludes the final element." in md


def test_omits_the_line_when_a_finding_is_not_line_specific():
    md = render_findings_md(ReviewResult(model="m1", findings=[_finding(line=None)]))
    assert "`src/app.py`" in md
    assert "src/app.py:" not in md


def test_renders_problem_trigger_fix_blocks_without_ids():
    md = render_findings_md(
        ReviewResult(model="m1", findings=[_finding(suggested_fix="Use range(n + 1).")])
    )
    assert "### **major** · bug — Off-by-one in the loop bound" in md
    assert "**Problem:** The range excludes the final element." in md
    assert "**Trigger:** Calling it with a one-element list." in md
    assert "**Fix:** Use range(n + 1)." in md
    assert "F1" not in md and "Suggested fix" not in md


def test_a_missing_trigger_and_fix_follow_the_block_rules():
    md = render_findings_md(ReviewResult(model="m1", findings=[_finding(trigger="")]))
    assert "**Trigger:** _not stated_" in md
    assert "**Fix:**" not in md


def test_a_fix_replacement_renders_as_a_code_block_with_a_longer_fence_when_needed():
    md = render_findings_md(
        ReviewResult(
            model="m1",
            findings=[
                _finding(fix_start_line=12, fix_end_line=12, replacement="for i in range(n + 1):")
            ],
        )
    )
    assert "`src/app.py:12`" in md
    assert "```python\nfor i in range(n + 1):\n```" in md
    fenced = render_findings_md(
        ReviewResult(
            model="m1",
            findings=[_finding(fix_start_line=1, fix_end_line=2, replacement="a\n```\nb")],
        )
    )
    assert "````python\na\n```\nb\n````" in fenced


def test_severity_orders_critical_first():
    """Three columns are read by scanning down; the worst thing must be at the top."""
    result = ReviewResult(
        model="m1",
        findings=[
            _finding(severity="minor", title="minor one"),
            _finding(severity="critical", title="critical one"),
            _finding(severity="major", title="major one"),
        ],
    )
    md = render_findings_md(result)
    assert md.index("critical one") < md.index("major one") < md.index("minor one")


def test_counts_findings():
    md = render_findings_md(ReviewResult(model="m1", findings=[_finding(), _finding()]))
    assert "2 findings" in md

    md_one = render_findings_md(ReviewResult(model="m1", findings=[_finding()]))
    assert "1 finding" in md_one and "1 findings" not in md_one


def test_a_clean_diff_says_so_rather_than_rendering_nothing():
    md = render_findings_md(ReviewResult(model="m1", findings=[]))
    assert "no issues" in md
    assert md.strip()  # an empty column would read as a broken page


def test_a_failed_model_renders_its_error_in_place():
    """A dead column still has to say why, or the reader assumes a clean review."""
    md = render_findings_md(ReviewResult(model="m1", error="AuthError: no API key"))
    assert "did not report" in md
    assert "AuthError: no API key" in md


def test_malformed_findings_are_named_under_the_ones_that_were_kept():
    md = render_findings_md(
        ReviewResult(
            model="m1",
            findings=[_finding()],
            malformed_findings=["findings[1] (src/b.py): severity: bad value"],
        )
    )
    assert "Off-by-one in the loop bound" in md
    assert "1 malformed finding set aside: findings[1] (src/b.py)" in md

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


def test_includes_a_suggested_fix_when_present():
    md = render_findings_md(
        ReviewResult(model="m1", findings=[_finding(suggested_fix="Use range(n + 1).")])
    )
    assert "Suggested fix" in md
    assert "Use range(n + 1)." in md


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

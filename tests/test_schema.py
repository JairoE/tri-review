"""Finding schema: trigger and the fix range / replacement fields (R2)."""

from tri_review.schema import Finding


def make(**overrides) -> Finding:
    base = dict(
        file="src/auth.py",
        line=42,
        severity="major",
        category="bug",
        title="Token refresh races logout",
        detail="refresh() writes the token after logout() cleared it.",
        trigger="Logout while a refresh request is in flight.",
    )
    base.update(overrides)
    return Finding(**base)


def test_missing_trigger_is_kept_as_empty():
    f = Finding.model_validate(
        {
            "file": "a.py",
            "severity": "minor",
            "category": "bug",
            "title": "t",
            "detail": "d",
        }
    )
    assert f.trigger == ""


def test_blank_trigger_is_kept_as_empty():
    assert make(trigger="   ").trigger == ""


def test_trigger_is_required_in_the_model_facing_schema():
    assert "trigger" in Finding.model_json_schema()["required"]


def test_end_line_defaults_to_start_line():
    f = make(fix_start_line=42, replacement="x = 1")
    assert (f.fix_start_line, f.fix_end_line) == (42, 42)


def test_valid_range_is_kept():
    f = make(fix_start_line=42, fix_end_line=44, replacement="x = 1")
    assert (f.fix_start_line, f.fix_end_line, f.replacement) == (42, 44, "x = 1")


def test_inverted_range_clears_fix_and_keeps_finding():
    f = make(fix_start_line=44, fix_end_line=42, replacement="x = 1")
    assert (f.fix_start_line, f.fix_end_line, f.replacement) == (None, None, None)
    assert f.title == "Token refresh races logout"


def test_replacement_without_start_line_clears_fix():
    f = make(replacement="x = 1")
    assert (f.fix_start_line, f.fix_end_line, f.replacement) == (None, None, None)


def test_zero_start_line_clears_fix():
    f = make(fix_start_line=0, fix_end_line=2, replacement="x = 1")
    assert (f.fix_start_line, f.fix_end_line, f.replacement) == (None, None, None)


def test_empty_replacement_is_a_deletion_not_null():
    f = make(fix_start_line=10, fix_end_line=12, replacement="")
    assert f.replacement == ""
    assert (f.fix_start_line, f.fix_end_line) == (10, 12)


def test_no_fix_fields_default_to_none():
    f = make()
    assert (f.fix_start_line, f.fix_end_line, f.replacement) == (None, None, None)


def test_old_history_findings_still_validate():
    f = Finding.model_validate(
        {
            "file": "a.py",
            "line": 3,
            "severity": "critical",
            "category": "security",
            "title": "t",
            "detail": "d",
            "suggested_fix": "do x",
        }
    )
    assert f.trigger == ""
    assert f.replacement is None

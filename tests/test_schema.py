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


# Models sometimes return numbers as JSON strings. Pydantic's lax mode turns
# those into ints only after the before-validator ran, so R2's range rules must
# be applied to the coerced values, and an unparseable value must cost only the
# fix, never the finding.


def _cleared(f: Finding) -> bool:
    return (f.fix_start_line, f.fix_end_line, f.replacement) == (None, None, None)


def test_string_inverted_range_clears_fix():
    assert _cleared(make(fix_start_line="44", fix_end_line="42", replacement="x = 1"))


def test_string_zero_start_line_clears_fix():
    assert _cleared(make(fix_start_line="0", fix_end_line="41", replacement="x = 1"))


def test_string_start_line_defaults_end_line():
    f = make(fix_start_line="3", replacement="x = 1")
    assert (f.fix_start_line, f.fix_end_line) == (3, 3)


def test_integer_valued_float_range_is_normalized():
    assert _cleared(make(fix_start_line=42.0, fix_end_line=40.0, replacement="x = 1"))
    f = make(fix_start_line=42.0, replacement="x = 1")
    assert (f.fix_start_line, f.fix_end_line) == (42, 42)


def test_boolean_coordinates_clear_fix():
    assert _cleared(make(fix_start_line=True, replacement="x = 1"))


def test_unparseable_start_line_keeps_finding_and_clears_fix():
    f = make(fix_start_line="L42", replacement="x = 1")
    assert _cleared(f)
    assert f.title == "Token refresh races logout"


def test_unparseable_end_line_keeps_finding_and_clears_fix():
    assert _cleared(make(fix_start_line=5, fix_end_line="end", replacement="x = 1"))
    assert _cleared(make(fix_start_line=5, fix_end_line=42.5, replacement="x = 1"))


def test_unparseable_fix_range_does_not_set_the_finding_aside():
    from tri_review.schema import ReviewOutput

    out = ReviewOutput.model_validate(
        {
            "findings": [
                {
                    "file": "a.py",
                    "line": 3,
                    "severity": "major",
                    "category": "bug",
                    "detail": "d",
                    "trigger": "t",
                    "fix_start_line": "unknown",
                    "replacement": "x",
                }
            ]
        }
    )
    assert len(out.findings) == 1 and out.malformed == []
    assert _cleared(out.findings[0])

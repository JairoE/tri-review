"""The reviewer cache is exact by construction -- these pin down what "exact" covers."""

import json
import os
import time

from tri_review import cache
from tri_review.schema import Finding, ReviewResult


def _result(model="gpt-5.6-terra"):
    return ReviewResult(
        model=model,
        findings=[
            Finding(
                file="auth.py",
                line=4,
                severity="critical",
                category="security",
                title="MD5 password hash",
                detail="Unsalted and fast.",
            )
        ],
    )


BASE = ("gpt-5.6-terra", "the payload", "the prompt", "the schema")


def test_every_input_participates_in_the_key():
    """A near-identical payload must miss. Similarity is not a hit here."""
    base = cache.key_for(*BASE)
    assert base != cache.key_for("claude-sonnet-5", *BASE[1:])
    assert base != cache.key_for(BASE[0], "the payloae", *BASE[2:])
    assert base != cache.key_for(*BASE[:2], "the prompt v2", BASE[3])
    assert base != cache.key_for(*BASE[:3], "the schema, widened")


def test_fields_cannot_run_together_into_the_same_key():
    """Without a separator, ("ab","c") and ("a","bc") would hash identically."""
    assert cache.key_for("ab", "c", "p", "s") != cache.key_for("a", "bc", "p", "s")


def test_a_stored_review_comes_back(tmp_path, monkeypatch):
    key = cache.key_for(*BASE)
    cache.save(key, _result())
    hit = cache.load(key)
    assert hit is not None
    assert hit.findings[0].title == "MD5 password hash"


def test_a_miss_is_none_not_an_error():
    assert cache.load("0" * 64) is None


def test_failures_are_never_stored():
    """Caching a failure would make the retry return the same failure for free."""
    key = cache.key_for("m", "p", "pr", "s")
    cache.save(key, ReviewResult(model="m", error="Connection error."))
    assert cache.load(key) is None


def test_an_expired_entry_is_a_miss(monkeypatch):
    monkeypatch.setenv("TRI_REVIEW_CACHE_TTL_DAYS", "1")
    key = cache.key_for(*BASE)
    cache.save(key, _result())
    path = cache._directory() / f"{key}.json"
    stale = time.time() - (3 * 86400)
    os.utime(path, (stale, stale))
    assert cache.load(key) is None


def test_expiry_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("TRI_REVIEW_CACHE_TTL_DAYS", "0")
    key = cache.key_for(*BASE)
    cache.save(key, _result())
    path = cache._directory() / f"{key}.json"
    ancient = time.time() - (900 * 86400)
    os.utime(path, (ancient, ancient))
    assert cache.load(key) is not None


def test_a_corrupt_entry_is_a_miss():
    key = cache.key_for(*BASE)
    path = cache._directory() / f"{key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ truncated", encoding="utf-8")
    assert cache.load(key) is None


def test_a_future_schema_is_a_miss():
    """The payload is deliberately valid under today's model.

    An earlier version of this test used `{}` as the result, which fails
    validation for an unrelated reason -- so it passed while the schema version
    was never checked at all. A future entry that still parses is exactly the
    case that must be rejected.
    """
    key = cache.key_for(*BASE)
    path = cache._directory() / f"{key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema": 99, "result": {"model": "m", "findings": []}}),
        encoding="utf-8",
    )
    assert cache.load(key) is None


def test_an_entry_with_no_schema_at_all_is_a_miss():
    key = cache.key_for(*BASE)
    path = cache._directory() / f"{key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"result": {"model": "m", "findings": []}}), encoding="utf-8")
    assert cache.load(key) is None

import pytest

from tri_review import config


def test_defaults():
    assert config.token_budget() == 100_000
    assert config.model_timeout() == 120
    assert config.model_a() == config.DEFAULT_MODEL_A


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("TRI_REVIEW_TOKEN_BUDGET", "5000")
    monkeypatch.setenv("TRI_REVIEW_TIMEOUT", "30")
    monkeypatch.setenv("TRI_REVIEW_MODEL_A", "some-other-model")
    assert config.token_budget() == 5000
    assert config.model_timeout() == 30
    assert config.model_a() == "some-other-model"


def test_blank_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("TRI_REVIEW_TOKEN_BUDGET", "")
    monkeypatch.setenv("TRI_REVIEW_MODEL_A", "")
    assert config.token_budget() == 100_000
    assert config.model_a() == config.DEFAULT_MODEL_A


def test_non_integer_env_raises(monkeypatch):
    monkeypatch.setenv("TRI_REVIEW_TIMEOUT", "soon")
    with pytest.raises(ValueError):
        config.model_timeout()


def test_estimate_tokens():
    assert config.estimate_tokens("a" * 400) == 100


def test_readme_documents_the_real_defaults():
    """The README's configuration table must match config.py.

    These defaults are the only place a user learns which models a review
    actually costs them, so a stale row is a wrong answer rather than an
    untidy one -- and the table drifted exactly once already, when the Google
    slot moved off gemini-3.7-flash and the row did not follow. Only the rows
    whose default is a literal value are checked; the ones documented as
    "same as model A" or "see DEFAULT_EXCLUDES" have no single value to pin.
    """
    import re
    from pathlib import Path

    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    documented = {
        var: default
        for var, default in re.findall(
            r"^\| `(TRI_REVIEW_\w+)` \| `([^`]+)` \|", readme, re.MULTILINE
        )
    }
    expected = {
        "TRI_REVIEW_MODEL_A": config.DEFAULT_MODEL_A,
        "TRI_REVIEW_MODEL_B": config.DEFAULT_MODEL_B,
        "TRI_REVIEW_MODEL_C": config.DEFAULT_MODEL_C,
        "TRI_REVIEW_TIMEOUT": str(config.model_timeout()),
        "TRI_REVIEW_TOKEN_BUDGET": str(config.token_budget()),
    }
    for var, value in expected.items():
        assert var in documented, f"{var} is no longer documented in the README table"
        assert documented[var] == value, (
            f"README documents {var} as {documented[var]!r} but config.py uses {value!r}"
        )


def test_google_timeout_is_longer_by_default():
    """Only the Google slot gets the longer deadline.

    It runs at thinking_level=high, which measured 90-155s on a large diff --
    over the 120s the other two providers are fine with. Raising the global
    default instead would make a hung OpenAI or Anthropic call take five
    minutes to fail rather than two.
    """
    assert config.model_timeout() == 120
    assert config.google_timeout() == 300


def test_an_explicit_timeout_still_applies_to_every_provider(monkeypatch):
    """A user who sets TRI_REVIEW_TIMEOUT keeps one number to reason about.

    The scoped default exists to avoid surprising anyone who never touched the
    setting; it must not override someone who did.
    """
    monkeypatch.setenv("TRI_REVIEW_TIMEOUT", "45")
    assert config.model_timeout() == 45
    assert config.google_timeout() == 45


def test_a_blank_timeout_does_not_count_as_explicit(monkeypatch):
    """An empty env var is how CI passes "unset", not a request for 0s."""
    monkeypatch.setenv("TRI_REVIEW_TIMEOUT", "")
    assert config.model_timeout() == 120
    assert config.google_timeout() == 300

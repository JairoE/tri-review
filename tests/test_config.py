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

"""Runtime configuration. Every value is overridable via a TRI_REVIEW_* env var."""

from __future__ import annotations

import os

# Model IDs, one per reviewer slot.
#
# gemini-2.5-pro was retired for new API users (404, "no longer available to
# new users" -- Google's own error names gemini-3.1-pro-preview as the
# replacement). Provider model lists move fast; if a default 404s again,
# override it with TRI_REVIEW_MODEL_C rather than assuming the code is wrong.
#
# None of these accept a custom `temperature`: the current OpenAI and Anthropic
# flagships reject sampling parameters outright, so tri-review never sends one.
DEFAULT_MODEL_A = "gpt-5.1"
DEFAULT_MODEL_B = "claude-opus-5"
DEFAULT_MODEL_C = "gemini-3.1-pro-preview"

# Rough char-per-token ratio used to estimate context size without a tokenizer.
CHARS_PER_TOKEN = 4


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None


def model_a() -> str:
    return os.environ.get("TRI_REVIEW_MODEL_A") or DEFAULT_MODEL_A


def model_b() -> str:
    return os.environ.get("TRI_REVIEW_MODEL_B") or DEFAULT_MODEL_B


def model_c() -> str:
    return os.environ.get("TRI_REVIEW_MODEL_C") or DEFAULT_MODEL_C


def synthesizer_model() -> str:
    """Model that cross-references the reviews. Defaults to slot A."""
    return os.environ.get("TRI_REVIEW_SYNTHESIZER") or model_a()


def token_budget() -> int:
    """Max estimated tokens for diff + file context combined."""
    return _env_int("TRI_REVIEW_TOKEN_BUDGET", 100_000)


def model_timeout() -> int:
    """Per-model wall-clock timeout in seconds."""
    return _env_int("TRI_REVIEW_TIMEOUT", 120)


def estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN

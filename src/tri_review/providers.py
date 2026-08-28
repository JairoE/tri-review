"""Mapping model IDs to chat clients.

Kept apart from the graph nodes so the CLI can validate a `--model` flag without
importing the review machinery.
"""

from __future__ import annotations

import os

from . import config

PROVIDER_PREFIXES: dict[str, tuple[str, ...]] = {
    "openai": ("gpt-", "o1", "o3", "o4"),
    "anthropic": ("claude-",),
    "google": ("gemini",),
}


def provider_of(model_name: str) -> str | None:
    """Return the provider a model ID belongs to, or None if unrecognized."""
    for provider, prefixes in PROVIDER_PREFIXES.items():
        if model_name.startswith(prefixes):
            return provider
    return None


def _cleaned_api_key(env_var: str) -> dict[str, str]:
    """kwargs for an explicit, whitespace-stripped api_key, or {} if unset.

    A key sourced from a GitHub Actions secret, shell profile, or .env file
    can carry a trailing newline or stray whitespace from how it was set --
    invisible in any log (secrets are masked as `***`) and fatal in a way
    that looks nothing like a bad key. httpx/h11 refuses to send an HTTP
    header value containing one at all, failing every single attempt in
    under 50ms with an opaque `LocalProtocolError("Illegal header value
    b'***\\n'")` wrapped in a generic `APIConnectionError: Connection
    error.` -- indistinguishable from real network flakiness without reading
    the exception's `__cause__` (see nodes._describe_error). Stripped once
    here, close to the wire, rather than trusting three different provider
    SDKs' own env-var parsing to do it. Returns {} when the var is unset so
    each provider's own "missing key" error still fires normally -- this
    only defends a key that is present but malformed, not a missing one.
    """
    value = os.environ.get(env_var)
    return {} if value is None else {"api_key": value.strip()}


def build_llm(model_name: str):
    """Construct a chat model from its ID, choosing the provider by ID prefix.

    No temperature is set anywhere: the current OpenAI and Anthropic flagships
    reject sampling parameters outright.
    """
    timeout = config.model_timeout()
    provider = provider_of(model_name)

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=model_name, timeout=timeout, max_retries=1, **_cleaned_api_key("OPENAI_API_KEY")
        )
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        # Generous max_tokens: on current Anthropic models max_tokens caps
        # thinking plus response together, so a tight value truncates output.
        #
        # max_retries=2 (vs. 1 for the other two providers): kept as a small
        # cushion against genuine transient connectivity issues. The repeated
        # live failures that originally motivated this turned out to be a
        # malformed ANTHROPIC_API_KEY secret (see _cleaned_api_key above),
        # not flakiness -- retries can't fix a deterministic bad header, so
        # this alone was never the real fix, just a reasonable one to keep.
        return ChatAnthropic(
            model=model_name,
            timeout=timeout,
            max_retries=2,
            max_tokens=8000,
            **_cleaned_api_key("ANTHROPIC_API_KEY"),
        )
    if provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(
            model=model_name, timeout=timeout, max_retries=1, **_cleaned_api_key("GOOGLE_API_KEY")
        )
    raise ValueError(f"Unrecognized model ID {model_name!r} — cannot pick a provider.")

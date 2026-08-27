"""Mapping model IDs to chat clients.

Kept apart from the graph nodes so the CLI can validate a `--model` flag without
importing the review machinery.
"""

from __future__ import annotations

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


def build_llm(model_name: str):
    """Construct a chat model from its ID, choosing the provider by ID prefix.

    No temperature is set anywhere: the current OpenAI and Anthropic flagships
    reject sampling parameters outright.
    """
    timeout = config.model_timeout()
    provider = provider_of(model_name)

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=model_name, timeout=timeout, max_retries=1)
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        # Generous max_tokens: on current Anthropic models max_tokens caps
        # thinking plus response together, so a tight value truncates output.
        #
        # max_retries=2 (vs. 1 for the other two providers): this slot has hit
        # a bare APIConnectionError -- a network-layer connection failure, not
        # a timeout -- on two separate live runs (PR #6, PR #8), with no
        # corresponding Anthropic status-page incident either time. One extra
        # retry buys another backoff-and-retry cycle against that specific
        # flakiness without materially extending a failed run's wall time.
        return ChatAnthropic(model=model_name, timeout=timeout, max_retries=2, max_tokens=8000)
    if provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(model=model_name, timeout=timeout, max_retries=1)
    raise ValueError(f"Unrecognized model ID {model_name!r} — cannot pick a provider.")

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


# Gemini families that predate thinking_level and use thinking_budget instead.
# Matched with the trailing dot so "gemini-2" cannot swallow a hypothetical
# "gemini-20" -- and so no 3.x ID can ever match, whatever its suffix.
_PRE_THINKING_LEVEL = ("gemini-1.", "gemini-2.")


def _predates_thinking_level(model_name: str) -> bool:
    """True for a Gemini ID from a family documented to reject thinking_level."""
    return model_name.startswith(_PRE_THINKING_LEVEL)


def build_llm(model_name: str):
    """Construct a chat model from its ID, choosing the provider by ID prefix.

    No temperature is set anywhere: the current OpenAI and Anthropic flagships
    reject sampling parameters outright. Note this is not the same as Gemini
    running greedy -- langchain-google-genai defaults `temperature` to 0.7
    when unset, so that branch samples whatever we do. Pinning it to 0 was
    measured and barely moved the empty-result rate (80% vs 90%), so it is
    left alone rather than set for the appearance of determinism.
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

        # thinking_level="high" is load-bearing, not tuning, and it is the
        # second half of a two-part fix -- the first half is the model itself
        # (see config.DEFAULT_MODEL_C).
        #
        # The failure it addresses is silent: Gemini returns a schema-valid
        # `{"findings": []}` with finish_reason=STOP, indistinguishable from a
        # clean pass. It is not budget exhaustion, which is what the token
        # split looks like: finish_reason is STOP on every sample, so nothing
        # is truncated, and the thought summary (include_thoughts) reads as a
        # survey that ends by approving the diff -- a summary, not the raw
        # chain of thought, so evidence of how it concluded rather than proof
        # of what it examined. The 9-token answer is just the width of
        # `{"findings": []}`. The model under-deliberates and concludes
        # "clean". Measured over 68 live calls on one 20K-token diff:
        #
        #   thinking_level  low 100% empty | medium 100% | (default) 90% | high 0%
        #
        # A clean dose-response curve, so capping reasoning (thinking_level
        # low/medium, or an explicit thinking_budget) makes it strictly worse.
        # None of the other suspects moved it: an unstructured call with no
        # schema at all was still 80% empty, as was the raw SDK's older
        # response_schema path and method="function_calling" -- this is the
        # model's judgment, not constrained decoding. temperature is left alone
        # for the same reason (pinning it to 0 gave 80% vs 90%).
        #
        # On gemini-3.8-flash "high" still earns its place: at the default
        # level that model went empty on 1 of 3 runs of a large diff, and on
        # 0 of 7 with this set.
        #
        # It is not cheap. Measured on the same 20K-token diff, output tokens
        # per review go from ~3.4K (3.7-flash, default level) to ~40-51K --
        # call it 13x, nearly all of it reasoning, which bills as output. Input
        # tokens are unchanged. It also pushes a large-diff review to 90-155s,
        # which is why config.model_timeout defaults to 300.
        # Withheld only from the Gemini families known to predate it. Google
        # documents thinking_level as a Gemini 3+ setting, with 1.x/2.x using
        # thinking_budget instead, so on an account where those are callable
        # the parameter is very likely rejected. (It could not be checked
        # directly: gemini-2.5-* returns 404 on the key used for the
        # measurements above, with or without it.)
        #
        # A denylist of old families rather than an allowlist of new ones, on
        # purpose. The allowlist that suggests itself is wrong in the one
        # direction that matters -- "gemini-3.8-flash".startswith("gemini-3-")
        # is False -- so it would silently drop the setting for the default
        # model and restore the 90%-empty bug this exists to fix. Excluding
        # "gemini-1." and "gemini-2." cannot match any 3.x ID, and a model
        # this code has never heard of (a -latest alias, a future release)
        # gets the setting, which is the right default for a reviewer whose
        # failure mode is thinking too little.
        kwargs = {} if _predates_thinking_level(model_name) else {"thinking_level": "high"}
        return ChatGoogleGenerativeAI(
            model=model_name,
            timeout=config.google_timeout(),
            max_retries=1,
            **kwargs,
            **_cleaned_api_key("GOOGLE_API_KEY"),
        )
    raise ValueError(f"Unrecognized model ID {model_name!r} — cannot pick a provider.")

"""Mapping model specs to chat clients.

A model is named by a *spec*: `[provider:]model[@effort]`. The provider is
inferred from the model ID's prefix when it can be, and stated explicitly when
it cannot -- a fine-tune, a brand-new family, or a model deliberately outside
the inferred set. The effort suffix picks the reasoning level for that one
model; each provider has its own name for the setting and `build_llm` maps to
it.

Kept apart from the graph nodes so the CLI can validate a `--reviewer` flag
without importing the review machinery.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from . import config

PROVIDERS = ("openai", "anthropic", "google")

# Every effort level any supported provider accepts, so a typo is caught before
# the PR is fetched. Which of these a given model honours is the provider's
# call, made at request time: OpenAI documents minimal/low/medium/high,
# Anthropic low through max, Gemini minimal through high. A level a model does
# not support fails that one reviewer with the provider's own error, the same
# way a retired model ID does -- visibly, in the report's failure note.
EFFORT_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

PROVIDER_PREFIXES: dict[str, tuple[str, ...]] = {
    "openai": ("gpt-", "o1", "o3", "o4"),
    "anthropic": ("claude-",),
    # Gemini 3 only, deliberately. The Google reviewer is only reliable at
    # thinking_level="high" (see build_llm), and thinking_level is a Gemini 3
    # setting -- older families use thinking_budget and reject it. So an older
    # model is not a degraded option here, it is an unsupported one, and it is
    # rejected up front by the same check that catches a typo, before the PR is
    # fetched or any reviewer is paid for. Family-agnostic aliases such as
    # gemini-flash-latest are excluded for the same reason: nothing guarantees
    # what they resolve to.
    "google": ("gemini-3",),
}


@dataclass(frozen=True)
class ModelSpec:
    """One model as the user named it: which provider, which ID, how hard to think.

    `effort` is None when the spec carried no suffix, which means "whatever the
    provider does by default" -- except for Google, where `build_llm` pins
    high for the measured reason documented there.
    """

    provider: str
    model: str
    effort: str | None = None

    @classmethod
    def parse(cls, text: str) -> "ModelSpec":
        """Parse `[provider:]model[@effort]`, raising ValueError with the reason.

        The provider prefix is optional when the ID's own prefix identifies it,
        and overrides that inference when given. So `openai:my-finetune` routes
        a name nothing here recognises, and `google:gemini-2.5-pro` admits a
        model the inferred set deliberately leaves out.
        """
        raw = text.strip()
        if not raw:
            raise ValueError("empty model spec")

        explicit: str | None = None
        rest = raw
        if ":" in rest:
            head, _, tail = rest.partition(":")
            if head.lower() in PROVIDERS:
                explicit, rest = head.lower(), tail
            else:
                raise ValueError(
                    f"unknown provider {head!r} in {raw!r}; expected one of "
                    + ", ".join(PROVIDERS)
                )

        effort: str | None = None
        if "@" in rest:
            rest, _, level = rest.rpartition("@")
            effort = level.strip().lower()
            if effort not in EFFORT_LEVELS:
                raise ValueError(
                    f"unknown effort {level!r} in {raw!r}; expected one of "
                    + ", ".join(EFFORT_LEVELS)
                )

        model = rest.strip()
        if not model:
            raise ValueError(f"no model ID in {raw!r}")

        provider = explicit or _provider_by_prefix(model)
        if provider is None:
            supported = ", ".join(
                f"{name} ({', '.join(p + '*' for p in prefixes)})"
                for name, prefixes in PROVIDER_PREFIXES.items()
            )
            raise ValueError(
                f"unrecognized model ID {model!r}: no provider prefix matched "
                f"({supported}). Name the provider explicitly, e.g. "
                f"openai:{model}, to route it anyway."
            )
        return cls(provider=provider, model=model, effort=effort)

    def __str__(self) -> str:
        """The canonical spelling: what the cache, history, and report use.

        The provider is written out only when the ID alone would not imply it,
        so `openai:gpt-5.1` and `gpt-5.1` are the same spec and share a cache
        entry, while `openai:my-finetune` keeps the prefix it needs.
        """
        text = self.model
        if _provider_by_prefix(self.model) != self.provider:
            text = f"{self.provider}:{text}"
        if self.effort is not None:
            text = f"{text}@{self.effort}"
        return text


def _provider_by_prefix(model_name: str) -> str | None:
    for provider, prefixes in PROVIDER_PREFIXES.items():
        if model_name.startswith(prefixes):
            return provider
    return None


def provider_of(spec: str) -> str | None:
    """Return the provider a model spec belongs to, or None if unrecognized.

    Accepts the full spec syntax, so a stored `openai:my-finetune@high` is read
    back to the same provider it was built with.
    """
    try:
        return ModelSpec.parse(spec).provider
    except ValueError:
        return None


def caveat_for(spec: ModelSpec) -> str | None:
    """A warning worth printing before this spec is paid for, or None.

    Nothing here refuses anything. Each caveat names a configuration that was
    measured to fail *silently* -- an empty review that reads as a clean pass --
    so the person choosing it does so knowingly.
    """
    if spec.provider == "google" and spec.effort not in (None, "high"):
        return (
            f"{spec}: Gemini below thinking_level=high was measured returning an "
            "empty review on every run of a diff with known bugs (low 100%, "
            "medium 100%, default 90%, high 0%). An empty result from this "
            "reviewer is inconclusive, not a clean pass."
        )
    if spec.provider == "google" and not spec.model.startswith("gemini-3"):
        return (
            f"{spec}: only Gemini 3 accepts thinking_level, which the Google "
            "reviewer relies on to report anything at all. An older family "
            "will either reject the call or run at a level measured to return "
            "empty reviews."
        )
    return None


def _cleaned_api_key(*env_vars: str) -> dict[str, str]:
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
    SDKs' own env-var parsing to do it. Returns {} when no var is set at all
    so each provider's own "missing key" error still fires normally -- this
    only defends a key that is present but malformed, not a missing one.

    `env_vars` are tried in order and the first non-blank one wins, which is
    how a provider with a fallback variable is resolved here rather than by
    the SDK. The SDK cannot be left to do it: the Action sets GOOGLE_API_KEY
    to "" whenever its secret is absent, and langchain-google-genai takes any
    set value -- "" blocked the GEMINI_API_KEY fallback when passed explicitly,
    and "   " or "\\n" were read from the environment raw when not. When every
    var is set but blank, an explicit "" is passed for the same reason: left
    to itself the SDK would send the raw whitespace instead.
    """
    blank = False
    for env_var in env_vars:
        raw = os.environ.get(env_var)
        if raw is None:
            continue
        if raw.strip():
            return {"api_key": raw.strip()}
        blank = True
    return {"api_key": ""} if blank else {}


def build_llm(spec: str | ModelSpec):
    """Construct a chat model from its spec, choosing the client by provider.

    The effort suffix, when present, is passed under each provider's own name
    for it: `reasoning_effort` for OpenAI, `effort` (output_config.effort) for
    Anthropic, `thinking_level` for Gemini. With no suffix nothing is sent and
    the provider's default applies -- except Gemini, which is pinned to high
    for the measured reason below.

    No temperature is set anywhere: the current OpenAI and Anthropic flagships
    reject sampling parameters outright. Note this is not the same as Gemini
    running greedy -- langchain-google-genai defaults `temperature` to 0.7
    when unset, so that branch samples whatever we do. Pinning it to 0 was
    measured and barely moved the empty-result rate (80% vs 90%), so it is
    left alone rather than set for the appearance of determinism.
    """
    if isinstance(spec, str):
        try:
            spec = ModelSpec.parse(spec)
        except ValueError as exc:
            raise ValueError(
                f"Unrecognized model ID {spec!r} — cannot pick a provider. {exc}"
            ) from None
    timeout = config.model_timeout()
    provider, model_name, effort = spec.provider, spec.model, spec.effort

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=model_name,
            timeout=timeout,
            max_retries=1,
            **({"reasoning_effort": effort} if effort is not None else {}),
            **_cleaned_api_key("OPENAI_API_KEY"),
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
            **({"effort": effort} if effort is not None else {}),
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
        # which is why config.google_timeout defaults to 300.
        #
        # Passed unconditionally: the inferred prefix admits only Gemini 3 IDs,
        # all of which accept it. Measured across every Gemini 3 model this
        # account can list -- 3-flash-preview, 3.1-flash-lite, 3.1-pro-preview,
        # 3.7-flash, 3.8-flash -- each answered identically with and without it.
        # An explicit `@effort` overrides it; `caveat_for` says out loud what
        # that was measured to cost, and an explicit `google:` prefix is how an
        # older family gets here at all.
        return ChatGoogleGenerativeAI(
            model=model_name,
            timeout=config.google_timeout(),
            max_retries=1,
            thinking_level=effort if effort is not None else "high",
            # GOOGLE_API_KEY first: the precedence langchain-google-genai gives them.
            **_cleaned_api_key("GOOGLE_API_KEY", "GEMINI_API_KEY"),
        )
    raise ValueError(f"Unrecognized provider {provider!r} for {model_name!r}.")  # pragma: no cover

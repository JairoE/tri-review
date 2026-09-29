import pytest

from tri_review.providers import (
    EFFORT_LEVELS,
    ModelSpec,
    _cleaned_api_key,
    build_llm,
    caveat_for,
    provider_of,
)


def test_cleaned_api_key_absent_env_var_returns_empty_kwargs(monkeypatch):
    monkeypatch.delenv("SOME_KEY", raising=False)
    assert _cleaned_api_key("SOME_KEY") == {}


def test_cleaned_api_key_strips_trailing_newline(monkeypatch):
    """The exact failure mode found live: a GitHub Actions secret with a
    trailing newline made httpx refuse to send the x-api-key header at all,
    failing every attempt in under 50ms with an opaque LocalProtocolError."""
    monkeypatch.setenv("SOME_KEY", "sk-abc123\n")
    assert _cleaned_api_key("SOME_KEY") == {"api_key": "sk-abc123"}


def test_cleaned_api_key_strips_surrounding_whitespace(monkeypatch):
    monkeypatch.setenv("SOME_KEY", "  sk-abc123  \n")
    assert _cleaned_api_key("SOME_KEY") == {"api_key": "sk-abc123"}


class _CapturingClient:
    """Records the kwargs it was constructed with; makes no network calls."""

    last_kwargs: dict = {}

    def __init__(self, **kwargs):
        type(self).last_kwargs = kwargs


def test_build_llm_passes_stripped_anthropic_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xyz\n")
    monkeypatch.setattr("langchain_anthropic.ChatAnthropic", _CapturingClient)

    build_llm("claude-sonnet-5")

    assert _CapturingClient.last_kwargs["api_key"] == "sk-ant-xyz"


def test_build_llm_passes_stripped_openai_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-xyz\n")
    monkeypatch.setattr("langchain_openai.ChatOpenAI", _CapturingClient)

    build_llm("gpt-5.1")

    assert _CapturingClient.last_kwargs["api_key"] == "sk-oai-xyz"


def test_build_llm_passes_stripped_google_key(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "sk-goog-xyz\n")
    monkeypatch.setattr("langchain_google_genai.ChatGoogleGenerativeAI", _CapturingClient)

    build_llm("gemini-3.7-flash")

    assert _CapturingClient.last_kwargs["api_key"] == "sk-goog-xyz"


def test_build_llm_sets_high_thinking_level_for_google(monkeypatch):
    """Gemini under-reports without it, and the failure is silent.

    At the default thinking level gemini-3.7-flash returned a schema-valid
    `{"findings": []}` on 9 of 10 live calls against a diff the other two
    reviewers found 3 and 2 real findings in; at "high" it reported on 5 of 5.
    Nothing raises when this is missing -- the review just comes back empty --
    so it is asserted here rather than left to be noticed in production.
    """
    monkeypatch.setenv("GOOGLE_API_KEY", "sk-goog-xyz")
    monkeypatch.setattr("langchain_google_genai.ChatGoogleGenerativeAI", _CapturingClient)

    build_llm("gemini-3.7-flash")

    assert _CapturingClient.last_kwargs["thinking_level"] == "high"


def test_build_llm_omits_api_key_kwarg_when_env_var_unset(monkeypatch):
    """Unset stays unset -- the provider SDK's own "missing key" error must
    still fire normally; this only defends a key that is present but malformed."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("langchain_anthropic.ChatAnthropic", _CapturingClient)

    build_llm("claude-sonnet-5")

    assert "api_key" not in _CapturingClient.last_kwargs


@pytest.mark.parametrize(
    "model",
    [
        "gemini-3.8-flash",       # the default
        "gemini-3.7-flash",
        "gemini-3-flash-preview",  # hyphenated form: must not be missed
        "gemini-3.1-pro-preview",
    ],
)
def test_every_supported_gemini_gets_high_thinking(monkeypatch, model):
    """Every admitted Gemini ID is a Gemini 3 one, so all of them get it.

    Nothing raises when it is missing -- the review just comes back empty and
    reads as a clean pass -- so it is asserted for each form of ID rather than
    left to be noticed in production.
    """
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    monkeypatch.setattr("langchain_google_genai.ChatGoogleGenerativeAI", _CapturingClient)

    build_llm(model)

    assert _CapturingClient.last_kwargs.get("thinking_level") == "high"


@pytest.mark.parametrize(
    "model",
    ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-1.5-pro", "gemini-flash-latest"],
)
def test_gemini_without_thinking_level_support_is_not_a_supported_model(model):
    """Older families use thinking_budget and reject thinking_level.

    Rather than quietly running them without it -- which is the configuration
    measured returning empty on 9 of 10 runs -- they are refused. The alias is
    refused too: nothing guarantees which family it resolves to.
    """
    assert provider_of(model) is None
    with pytest.raises(ValueError, match="Unrecognized model ID"):
        build_llm(model)


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_cleaned_api_key_never_hands_the_sdk_raw_whitespace(monkeypatch, blank):
    """Set but blank passes an explicit "": omitting the kwarg would let the
    SDK read the raw "   " or "\\n" from the environment itself."""
    monkeypatch.setenv("SOME_KEY", blank)
    assert _cleaned_api_key("SOME_KEY") == {"api_key": ""}


def test_cleaned_api_key_takes_the_first_non_blank_var(monkeypatch):
    monkeypatch.setenv("FIRST", "  ")
    monkeypatch.setenv("SECOND", " k2\n")
    assert _cleaned_api_key("FIRST", "SECOND") == {"api_key": "k2"}
    monkeypatch.delenv("FIRST")
    monkeypatch.delenv("SECOND")
    assert _cleaned_api_key("FIRST", "SECOND") == {}


# Built through the real, installed client rather than a capturing stub: the
# live failure was in what langchain-google-genai does with the kwargs and the
# environment together, which a stub cannot show. Construction makes no call.


def _google_key(monkeypatch, **env):
    for var in ("GOOGLE_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    return build_llm("gemini-3.8-flash").google_api_key.get_secret_value()


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_a_blank_google_key_falls_back_to_gemini_in_the_real_client(monkeypatch, blank):
    assert _google_key(monkeypatch, GOOGLE_API_KEY=blank, GEMINI_API_KEY="gem") == "gem"


def test_google_key_keeps_precedence_over_gemini_in_the_real_client(monkeypatch):
    assert _google_key(monkeypatch, GOOGLE_API_KEY="goo", GEMINI_API_KEY="gem") == "goo"


def test_an_all_blank_google_key_fails_as_missing_in_the_real_client(monkeypatch):
    with pytest.raises(Exception, match="API key required"):
        _google_key(monkeypatch, GOOGLE_API_KEY="  \n")


# --- model specs: [provider:]model[@effort] ----------------------------------


def test_a_bare_id_infers_its_provider_and_carries_no_effort():
    spec = ModelSpec.parse("gpt-5.6-terra")
    assert spec == ModelSpec(provider="openai", model="gpt-5.6-terra", effort=None)
    assert str(spec) == "gpt-5.6-terra"


def test_an_effort_suffix_is_parsed_and_kept_in_the_canonical_form():
    spec = ModelSpec.parse("claude-sonnet-5@medium")
    assert spec.effort == "medium"
    assert str(spec) == "claude-sonnet-5@medium"


def test_an_explicit_provider_routes_an_id_nothing_recognises():
    """A fine-tune or a brand-new family has no prefix to infer from."""
    spec = ModelSpec.parse("openai:my-finetune@high")
    assert spec == ModelSpec(provider="openai", model="my-finetune", effort="high")
    assert str(spec) == "openai:my-finetune@high", "the prefix is load-bearing, so it stays"


def test_a_redundant_provider_prefix_canonicalises_away():
    """`openai:gpt-5.1` and `gpt-5.1` are one spec, so they share a cache entry."""
    assert str(ModelSpec.parse("openai:gpt-5.1")) == "gpt-5.1"
    assert ModelSpec.parse("openai:gpt-5.1") == ModelSpec.parse("gpt-5.1")


def test_an_explicit_provider_overrides_the_inferred_one():
    assert ModelSpec.parse("google:gemini-2.5-pro").provider == "google"
    assert ModelSpec.parse("anthropic:gpt-5.1").provider == "anthropic"


def test_effort_is_case_insensitive_and_trimmed():
    assert ModelSpec.parse(" gpt-5.1@HIGH ").effort == "high"


@pytest.mark.parametrize("bad", ["gpt-5.1@hihg", "gpt-5.1@", "llama:gpt-5.1", "@high", "", "  "])
def test_a_malformed_spec_is_rejected_with_a_reason(bad):
    with pytest.raises(ValueError):
        ModelSpec.parse(bad)


def test_an_unrecognised_id_says_how_to_route_it_anyway():
    with pytest.raises(ValueError, match="openai:llama-9000"):
        ModelSpec.parse("llama-9000")


def test_every_documented_effort_level_parses():
    for level in EFFORT_LEVELS:
        assert ModelSpec.parse(f"gpt-5.1@{level}").effort == level


def test_provider_of_reads_the_full_spec_syntax():
    assert provider_of("openai:my-finetune@high") == "openai"
    assert provider_of("gemini-3.8-flash@high") == "google"
    assert provider_of("llama-9000") is None


# --- effort reaches each client under its own name --------------------------


def test_openai_effort_is_passed_as_reasoning_effort(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setattr("langchain_openai.ChatOpenAI", _CapturingClient)

    build_llm("gpt-5.1@low")

    assert _CapturingClient.last_kwargs["reasoning_effort"] == "low"


def test_openai_without_a_suffix_sends_no_effort_at_all(monkeypatch):
    """No suffix means the provider's default, not a default of ours."""
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setattr("langchain_openai.ChatOpenAI", _CapturingClient)

    build_llm("gpt-5.1")

    assert "reasoning_effort" not in _CapturingClient.last_kwargs


def test_anthropic_effort_is_passed_as_effort(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr("langchain_anthropic.ChatAnthropic", _CapturingClient)

    build_llm("claude-opus-5@xhigh")

    assert _CapturingClient.last_kwargs["effort"] == "xhigh"


def test_anthropic_without_a_suffix_sends_no_effort_at_all(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr("langchain_anthropic.ChatAnthropic", _CapturingClient)

    build_llm("claude-opus-5")

    assert "effort" not in _CapturingClient.last_kwargs


def test_google_effort_overrides_the_pinned_thinking_level(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    monkeypatch.setattr("langchain_google_genai.ChatGoogleGenerativeAI", _CapturingClient)

    build_llm("gemini-3.8-flash@medium")

    assert _CapturingClient.last_kwargs["thinking_level"] == "medium"


def test_an_explicit_google_prefix_admits_an_older_family(monkeypatch):
    """Refused when inferred, routed when asked for by name."""
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    monkeypatch.setattr("langchain_google_genai.ChatGoogleGenerativeAI", _CapturingClient)

    build_llm("google:gemini-2.5-pro")

    assert _CapturingClient.last_kwargs["model"] == "gemini-2.5-pro"


def test_the_client_never_sees_the_spec_syntax(monkeypatch):
    """The provider gets a bare model ID, never `openai:` or `@high`."""
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setattr("langchain_openai.ChatOpenAI", _CapturingClient)

    build_llm("openai:my-finetune@high")

    assert _CapturingClient.last_kwargs["model"] == "my-finetune"


def test_build_llm_accepts_a_parsed_spec_too(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setattr("langchain_openai.ChatOpenAI", _CapturingClient)

    build_llm(ModelSpec(provider="openai", model="gpt-5.1", effort="high"))

    assert _CapturingClient.last_kwargs["reasoning_effort"] == "high"


# --- the real clients accept what build_llm sends ----------------------------
# Constructed through the installed libraries, not a stub: the claim under test
# is that each client takes the kwarg at all. Construction makes no call.


def test_the_real_openai_client_takes_reasoning_effort(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    assert build_llm("gpt-5.1@low").reasoning_effort == "low"


def test_the_real_anthropic_client_takes_effort(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert build_llm("claude-opus-5@medium").reasoning_effort == "medium"


def test_the_real_google_client_takes_thinking_level(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    assert build_llm("gemini-3.8-flash@low").reasoning_effort == "low"


# --- caveats: measured to fail silently, so said out loud --------------------


def test_gemini_below_high_carries_a_caveat():
    caveat = caveat_for(ModelSpec.parse("gemini-3.8-flash@low"))
    assert caveat is not None and "empty" in caveat


def test_gemini_at_high_or_unspecified_carries_none():
    assert caveat_for(ModelSpec.parse("gemini-3.8-flash@high")) is None
    assert caveat_for(ModelSpec.parse("gemini-3.8-flash")) is None


def test_an_older_gemini_family_carries_a_caveat():
    caveat = caveat_for(ModelSpec.parse("google:gemini-2.5-pro"))
    assert caveat is not None and "Gemini 3" in caveat


def test_other_providers_carry_no_caveat_at_any_effort():
    for level in EFFORT_LEVELS:
        assert caveat_for(ModelSpec.parse(f"gpt-5.1@{level}")) is None
        assert caveat_for(ModelSpec.parse(f"claude-opus-5@{level}")) is None


# --- review findings on PR #25 -----------------------------------------------


@pytest.mark.parametrize("bad", ["gpt-5.1@high@low", "gpt-5.1@@high", "openai:m@x@high"])
def test_a_spec_takes_at_most_one_effort_suffix(bad):
    """`gpt-5.1@high@low` used to parse as a model named `gpt-5.1@high`."""
    with pytest.raises(ValueError, match="more than one '@'"):
        ModelSpec.parse(bad)


def test_a_bare_openai_fine_tune_id_is_recognised():
    """Fine-tune IDs are colon-separated; `ft` is not a provider name."""
    spec = ModelSpec.parse("ft:gpt-4o-mini:acme::abc123@low")
    assert spec == ModelSpec(provider="openai", model="ft:gpt-4o-mini:acme::abc123", effort="low")
    assert str(spec) == "ft:gpt-4o-mini:acme::abc123@low"
    assert ModelSpec.parse(str(spec)) == spec, "the canonical form parses back to itself"


def test_an_explicit_provider_still_works_in_front_of_a_colon_id():
    spec = ModelSpec.parse("openai:ft:gpt-4o-mini:acme::abc123")
    assert spec.model == "ft:gpt-4o-mini:acme::abc123"


def test_an_unknown_word_before_a_colon_is_part_of_the_id_not_a_provider():
    with pytest.raises(ValueError, match="unrecognized model ID 'llama:gpt-5.1'"):
        ModelSpec.parse("llama:gpt-5.1")


# What each provider is actually sent. Built through the installed clients and
# read from the request payload they would put on the wire, so the claim under
# test is "the API receives this effort", not "some attribute holds it".


def _messages():
    from langchain_core.messages import HumanMessage

    return [HumanMessage(content="review this")]


def test_openai_sends_reasoning_effort_on_the_wire(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    payload = build_llm("gpt-5.1@low")._get_request_payload(_messages())
    assert payload["reasoning_effort"] == "low"


def test_openai_without_effort_sends_none(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    payload = build_llm("gpt-5.1")._get_request_payload(_messages())
    assert payload.get("reasoning_effort") is None


def test_anthropic_sends_effort_as_output_config_on_the_wire(monkeypatch):
    """Answers the review's doubt: the installed client takes `effort` and
    translates it into the API's own output_config.effort."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    payload = build_llm("claude-opus-5@medium")._get_request_payload(_messages())
    assert payload["output_config"] == {"effort": "medium"}


def test_anthropic_with_effort_gets_room_for_the_thinking_it_turns_on(monkeypatch):
    """Effort switches on adaptive thinking, which shares max_tokens with the
    answer. The 8000 sized for a no-thinking call would truncate it."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    payload = build_llm("claude-opus-5@high")._get_request_payload(_messages())
    assert payload["thinking"]["type"] == "adaptive"
    assert payload["max_tokens"] > 8000


def test_anthropic_without_effort_is_the_call_it_always_was(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    payload = build_llm("claude-sonnet-5")._get_request_payload(_messages())
    assert payload["max_tokens"] == 8000
    assert payload.get("thinking") is None
    assert payload.get("output_config") is None


@pytest.mark.parametrize("spec, level", [("gemini-3.8-flash@low", "LOW"), ("gemini-3.8-flash", "HIGH")])
def test_google_sends_thinking_level_on_the_wire(monkeypatch, spec, level):
    """Answers the review's doubt: `reasoning_effort` and `thinking_level` are
    one field in the installed client, and the request carries it."""
    monkeypatch.setenv("GOOGLE_API_KEY", "k")
    llm = build_llm(spec)
    assert llm.thinking_level == llm.reasoning_effort
    request = llm._prepare_request(_messages())
    assert request["config"].thinking_config.thinking_level.value == level

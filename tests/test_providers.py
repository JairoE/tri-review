import pytest

from tri_review.providers import _cleaned_api_key, build_llm, provider_of


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
def test_cleaned_api_key_treats_a_blank_value_as_unset(monkeypatch, blank):
    """The Action sets GOOGLE_API_KEY to "" when the secret is missing, and an
    explicit api_key="" stops langchain-google-genai from falling back to
    GEMINI_API_KEY -- a run with a key failed as though it had none."""
    monkeypatch.setenv("SOME_KEY", blank)
    assert _cleaned_api_key("SOME_KEY") == {}

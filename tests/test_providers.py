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

from tri_review.providers import _cleaned_api_key, build_llm


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


def test_build_llm_omits_api_key_kwarg_when_env_var_unset(monkeypatch):
    """Unset stays unset -- the provider SDK's own "missing key" error must
    still fire normally; this only defends a key that is present but malformed."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("langchain_anthropic.ChatAnthropic", _CapturingClient)

    build_llm("claude-sonnet-5")

    assert "api_key" not in _CapturingClient.last_kwargs

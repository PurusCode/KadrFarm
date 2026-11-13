from kadr_farm import config


def test_provider_timeouts_have_fast_primary_and_long_local_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setenv("LLM_PRIMARY_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("LLM_PRIMARY_API_KEY", "test-primary")
    monkeypatch.setenv("LLM_PRIMARY_MODEL", "primary-model")
    monkeypatch.setenv("LLM_SECONDARY_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("LLM_SECONDARY_API_KEY", "ollama")
    monkeypatch.setenv("LLM_SECONDARY_MODEL", "qwen2.5:14b-instruct")

    settings = config.load_settings()

    assert [p.timeout_seconds for p in settings.providers] == [120.0, 900.0]


def test_provider_timeout_can_be_overridden_and_is_range_checked(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setenv("LLM_PRIMARY_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("LLM_PRIMARY_API_KEY", "test-primary")
    monkeypatch.setenv("LLM_PRIMARY_MODEL", "primary-model")
    monkeypatch.setenv("LLM_PRIMARY_TIMEOUT_SECONDS", "75")

    settings = config.load_settings()
    assert settings.providers[0].timeout_seconds == 75.0

    monkeypatch.setenv("LLM_PRIMARY_TIMEOUT_SECONDS", "5")
    try:
        config.load_settings()
    except ValueError as exc:
        assert "10..3600" in str(exc)
    else:
        raise AssertionError("Out-of-range timeout must be rejected")


def test_provider_specific_timeouts_are_scoped_to_structured_router(monkeypatch):
    import sys
    from types import SimpleNamespace
    from kadr_farm.llm import LLMRouter
    from kadr_farm.config import ProviderConfig

    captured = []

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.append(kwargs)

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))
    settings = SimpleNamespace(providers=[
        ProviderConfig("primary", "https://example.invalid/v1", "key", "mws", 120),
        ProviderConfig("secondary", "http://localhost:11434/v1", "ollama", "qwen", 900),
    ])

    LLMRouter(settings)
    assert [entry["timeout"] for entry in captured] == [600.0, 600.0]

    captured.clear()
    LLMRouter(settings, honor_provider_timeouts=True)
    assert [entry["timeout"] for entry in captured] == [120.0, 900.0]

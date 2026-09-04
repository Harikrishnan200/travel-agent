"""Settings load correctly and defaults are safe (offline by default)."""

from app.config import Settings, get_settings


def test_defaults_are_offline_safe():
    settings = Settings(_env_file=None)
    assert settings.use_mock_data is True
    assert settings.primary_llm_provider == "groq"
    assert settings.fallback_llm_provider == "gemini"


def test_env_overrides_defaults(monkeypatch):
    monkeypatch.setenv("USE_MOCK_DATA", "false")
    monkeypatch.setenv("PRIMARY_LLM_PROVIDER", "gemini")
    settings = Settings(_env_file=None)
    assert settings.use_mock_data is False
    assert settings.primary_llm_provider == "gemini"


def test_get_settings_is_cached():
    assert get_settings() is get_settings()

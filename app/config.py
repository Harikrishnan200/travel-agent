"""Application settings, loaded once from environment / .env."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"

    # LLM
    primary_llm_provider: str = "groq"
    fallback_llm_provider: str = "gemini"
    groq_api_key: str = ""
    gemini_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"
    gemini_model: str = "gemini-2.0-flash"

    # Infrastructure
    database_url: str = "postgresql+psycopg://travel:travel@localhost:5432/travel"
    redis_url: str = "redis://localhost:6379/0"

    # MCP
    mcp_gateway_url: str = "http://localhost:9000"
    mcp_gateway_api_key: str = "dev-gateway-key"
    tool_rate_limit_per_minute: int = 60

    # Data source
    use_mock_data: bool = True

    # Planning limits (used by the validator / re-planner later)
    max_replan_attempts: int = 3
    max_trip_days: int = 30
    max_travelers: int = 20


@lru_cache
def get_settings() -> Settings:
    """Cached so every module shares one Settings instance."""
    return Settings()

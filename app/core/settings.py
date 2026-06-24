"""Centralized config. Read once at startup, validated."""
from __future__ import annotations
from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Supabase
    supabase_url: str
    supabase_service_role_key: str
    supabase_anon_key: str

    # Anthropic
    anthropic_api_key: str
    claude_model_opener: str = "claude-sonnet-4-6"
    claude_model_agent: str = "claude-sonnet-4-6"
    claude_model_qualifier: str = "claude-haiku-4-5-20251001"

    # Encryption
    fernet_key: str

    # Discord
    discord_bot_token: str = ""
    discord_guild_id: str = ""
    discord_warming_queue_channel_id: str = ""
    discord_handoff_channel_id: str = ""
    discord_alerts_channel_id: str = ""
    discord_metrics_channel_id: str = ""

    # Proxy defaults
    default_proxy_user: str = ""
    default_proxy_pass: str = ""
    default_proxy_host: str = ""
    default_proxy_port: str = ""

    # Operational
    environment: str = "production"
    log_level: str = "INFO"
    timezone: str = "Asia/Manila"
    rate_limit_safety_factor: float = Field(default=0.7, ge=0.1, le=1.0)

    # Test mode — for solo/burner-account validation before going to scale
    test_mode: bool = False
    test_mode_profile_limit: int = 10   # stop discovery after this many accounts

    # LLM mode — 'api' uses Anthropic API; 'manual_paste' writes prompts to a
    # queue table for you to handle via Claude.ai (Claude Max subscription).
    llm_mode: str = Field(default="api", pattern="^(api|manual_paste)$")

    # Cross-platform
    youtube_api_key: str = ""

    # n8n
    n8n_webhook_base: str = ""
    n8n_api_key: str = ""

    # Dashboard
    dashboard_password: str = ""
    dashboard_url: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]

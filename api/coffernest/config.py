"""Runtime settings from environment variables."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"
    port: int = 8000

    # Postgres: hosting panels often give a postgres:// URL; we normalise it to the asyncpg driver.
    database_url: str = "postgresql+asyncpg://coffernest:coffernest@localhost:5433/coffernest"

    # S3 / MinIO
    s3_endpoint: str = "http://localhost:9000"
    s3_public_url: str = "http://localhost:9000"
    s3_region: str = "us-east-1"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"
    s3_bucket: str = "coffernest"

    # Auth
    jwt_secret: str = Field(default="dev-only-change-me-dev-only-change-me", min_length=32)
    access_ttl_s: int = 15 * 60
    refresh_ttl_s: int = 30 * 86_400
    cookie_domain: str | None = None
    cors_origins: list[str] = ["http://localhost:5173"]
    # Open sign-up for anyone. Off: the first account on the server signs up freely and becomes
    # its admin; after that, only emails the admin added to their family can sign up.
    allow_registration: bool = False
    # A demo account the server creates (and re-creates) by itself, with a year of sample books.
    # It lives in its own household and never takes the admin seat: the first real sign-up
    # still becomes this server's admin. Off unless all three are set.
    seed_demo: bool = False
    demo_email: str = ""
    demo_password: str = ""
    # Start the demo's books afresh this often, so "this month" always has entries.
    demo_refresh_days: int = 30
    # Header carrying the visitor's IP from a trusted reverse proxy (e.g. CF-Connecting-IP,
    # X-Real-IP). Empty = use the socket address; never trust a header the proxy doesn't set.
    trusted_proxy_header: str = ""

    # Web push (VAPID). Empty = push disabled.
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_subject: str = "mailto:owner@example.com"

    # AI
    ai_provider: Literal["none", "claude_agent", "openai_compat"] = "none"
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4.1-mini"
    claude_model: str = "claude-opus-5-5"

    # Public origin (OAuth issuer, MCP resource URL) and the key for remembered PDF passwords.
    public_url: str = "http://localhost:8000"
    secret_enc_key: str = ""
    mcp_enabled: bool = True
    # Shown to apps before sign-in. Empty = this server's own /privacy and /terms pages.
    privacy_url: str = ""
    terms_url: str = ""
    # Inbox: extracted rows at or above this confidence go straight into the books.
    auto_add_confidence: float = 0.85

    # Domain defaults
    # Run the background jobs inside the API process (one container on a small server).
    run_worker: bool = False

    timezone: str = "Asia/Kolkata"
    materialize_days_ahead: int = 60

    @property
    def async_database_url(self) -> str:
        url = self.database_url
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+asyncpg://" + url[len(prefix) :]
        return url


def _clean_secret_env() -> None:
    """Long tokens pasted from a terminal often pick up the line-wrap as a space or newline.
    The Claude CLI reads these straight from the environment, so tidy them before it starts."""
    for key in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "LLM_API_KEY"):
        value = os.environ.get(key)
        if value:
            os.environ[key] = "".join(value.split()).strip("\"'")


@lru_cache
def get_settings() -> Settings:
    _clean_secret_env()
    return Settings()

"""Application configuration, loaded from environment / .env.

All settings are typed and validated by pydantic-settings. Nothing sensitive is
hard-coded — secrets come from the environment or the gitignored ``keys/`` dir.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- App ----
    app_name: str = "Herald"
    environment: str = "development"
    debug: bool = True
    api_v1_prefix: str = "/api/v1"
    backend_cors_origins: str = "http://localhost:5173,http://localhost:3000"
    frontend_url: str = "http://localhost:5173"

    # ---- Security / JWT ----
    jwt_secret: str = "change-me-to-a-long-random-string"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 1440
    # Fernet key protecting stored platform API tokens at rest. Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # Blank means tokens are stored in the clear — fine for local dev, refused
    # in production (see app.services.crypto).
    token_encryption_key: str = ""

    # ---- Database ----
    database_url: str = "postgresql+psycopg://herald:herald@localhost:5432/herald"

    # ---- Redis / Celery ----
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"
    # When False, generation and publishing run inline in the request instead of
    # being handed to a worker. Intended for single-process deployments and
    # tests; production runs workers and leaves this on.
    celery_enabled: bool = True

    # ---- AI (OpenRouter — free models only) ----
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # The workhorse: fast and cheap enough to draft a post per repo push.
    openrouter_model: str = "openai/gpt-oss-20b:free"
    # The bigger free model, used when a piece needs the extra headroom —
    # long-form tutorials and comparisons rather than a 280-character tweet.
    openrouter_long_form_model: str = "openai/gpt-oss-120b:free"
    openrouter_app_url: str = "https://herald.local"
    openrouter_app_title: str = "Herald"

    # ---- AI fallback providers ---------------------------------------------
    # Tried in order after OpenRouter. All speak the OpenAI chat-completions
    # dialect, so one client handles the lot; a provider with no key configured
    # is simply skipped. Free tiers all round.
    gemini_api_key: str = ""
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    gemini_model: str = "gemini-2.0-flash"

    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_model: str = "llama-3.3-70b-versatile"

    cerebras_api_key: str = ""
    cerebras_base_url: str = "https://api.cerebras.ai/v1"
    cerebras_model: str = "llama-3.3-70b"

    # Circuit breaker: after this many consecutive failures a provider is
    # skipped entirely for the cool-down, so one dead upstream costs a timeout
    # once rather than on every request.
    llm_breaker_threshold: int = 3
    llm_breaker_cooldown_seconds: int = 300

    # ---- GitHub (project change monitoring) ----
    # A classic or fine-grained PAT with `repo` read. Optional: without it the
    # monitor still works against public repos at the anonymous rate limit
    # (60/hour), which is enough for a handful of projects on a slow cadence.
    github_token: str = ""
    github_api_url: str = "https://api.github.com"
    github_timeout_seconds: float = 15.0

    # ---- Autopilot ----
    # How often the beat task sweeps registered repos for new commits/releases.
    autopilot_scan_interval_seconds: int = 3600
    # How often the beat task looks for scheduled content that has come due.
    publish_scan_interval_seconds: int = 300
    # How often published posts are re-polled for view/reaction counts.
    metrics_scan_interval_seconds: int = 21600
    # A release, or this many commits since the last generated piece, is what
    # counts as "worth writing about". Below it the autopilot stays quiet.
    autopilot_commit_threshold: int = 10
    # Generated content at or above this confidence may publish without review
    # when the project is set to auto-publish. Below it, everything queues.
    autopilot_auto_publish_confidence: float = 0.8
    # Ceiling on autopilot drafts per project per day, so a busy repo can't
    # turn into a content firehose.
    autopilot_daily_content_limit: int = 2

    # ---- Publishing ----
    # Requests to platform APIs. Publishing is a background task, so a generous
    # timeout is cheaper than a retry.
    publish_timeout_seconds: float = 30.0
    # A publication that fails is retried this many times with backoff before it
    # is parked as `failed` for a human to look at.
    publish_max_retries: int = 3

    @field_validator(
        "access_token_expire_minutes",
        "autopilot_daily_content_limit",
        "publish_max_retries",
    )
    @classmethod
    def _positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("must be positive")
        return v

    @field_validator("autopilot_auto_publish_confidence")
    @classmethod
    def _unit_interval(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("must be between 0 and 1")
        return v

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.backend_cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (env is read once per process)."""
    return Settings()


settings = get_settings()

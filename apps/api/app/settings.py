from functools import lru_cache

from pydantic import AnyHttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigurationError(ValueError):
    """Raised when a deployment setting is missing or internally inconsistent."""


class Settings(BaseSettings):
    app_env: str = "development"
    app_name: str = "SignalForge API"
    # Empty defaults keep a source checkout safe to run without infrastructure.
    # Deployments must opt in explicitly through DATABASE_URL/REDIS_URL.
    database_url: str = ""
    redis_url: str = ""
    repository_backend: str = "memory"
    queue_backend: str = "memory"
    migrations_path: str = "migrations"
    migrate_on_startup: bool = False
    log_level: str = "INFO"
    frontend_origin: AnyHttpUrl = "http://localhost:5173"
    auth_mode: str = "development"
    auth0_domain: str = ""
    auth0_audience: str = ""
    classifier_backend: str = "fake"
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-2.5-flash"
    gemini_timeout_seconds: float = 30.0
    slack_backend: str = "fake"
    slack_enabled: bool = False
    slack_bot_token: SecretStr | None = None
    slack_api_url: AnyHttpUrl = "https://slack.com/api"
    rate_limit_per_minute: int = 120
    force_json_logging: bool = False
    allow_private_crawl_destinations: bool = False

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


def secret_value(value: SecretStr | None) -> str:
    """Return a configured secret without ever exposing it through repr/logging."""
    return value.get_secret_value().strip() if value is not None else ""


def validate_startup_settings(settings: Settings) -> None:
    """Validate settings that would otherwise fail only after the app starts."""
    app_env = settings.app_env.lower()
    repository_backend = settings.repository_backend.lower()
    queue_backend = settings.queue_backend.lower()
    classifier_backend = settings.classifier_backend.lower()
    slack_backend = settings.slack_backend.lower()

    if repository_backend not in {"memory", "postgres", "postgresql", "sqlalchemy"}:
        raise ConfigurationError(
            "REPOSITORY_BACKEND must be memory or postgres; update REPOSITORY_BACKEND."
        )
    if queue_backend not in {"memory", "redis"}:
        raise ConfigurationError("QUEUE_BACKEND must be memory or redis; update QUEUE_BACKEND.")
    if classifier_backend not in {"fake", "gemini"}:
        raise ConfigurationError(
            "CLASSIFIER_BACKEND must be fake or gemini; update CLASSIFIER_BACKEND."
        )
    if slack_backend not in {"fake", "slack"}:
        raise ConfigurationError("SLACK_BACKEND must be fake or slack; update SLACK_BACKEND.")
    if settings.gemini_timeout_seconds <= 0:
        raise ConfigurationError(
            "GEMINI_TIMEOUT_SECONDS must be greater than zero; update GEMINI_TIMEOUT_SECONDS."
        )
    if app_env == "production" and repository_backend in {"postgres", "postgresql", "sqlalchemy"}:
        if not settings.database_url.strip():
            raise ConfigurationError(
                "DATABASE_URL is required in production when REPOSITORY_BACKEND=postgres."
            )
    if queue_backend == "redis" and not settings.redis_url.strip():
        raise ConfigurationError("REDIS_URL is required when QUEUE_BACKEND=redis.")
    if classifier_backend == "gemini" and not secret_value(settings.gemini_api_key):
        raise ConfigurationError("GEMINI_API_KEY is required when CLASSIFIER_BACKEND=gemini.")
    if app_env == "production" and classifier_backend != "gemini":
        raise ConfigurationError(
            "CLASSIFIER_BACKEND=gemini is required in production; the fake classifier "
            "is only available in development and test."
        )
    slack_configured = settings.slack_enabled or slack_backend == "slack"
    if slack_configured and not secret_value(settings.slack_bot_token):
        raise ConfigurationError("SLACK_BOT_TOKEN is required when Slack delivery is enabled.")
    if settings.auth_mode == "production":
        if not settings.auth0_domain.strip():
            raise ConfigurationError("AUTH0_DOMAIN is required in production auth mode.")
        if not settings.auth0_audience.strip():
            raise ConfigurationError("AUTH0_AUDIENCE is required in production auth mode.")


@lru_cache
def get_settings() -> Settings:
    return Settings()

from functools import lru_cache
from typing import Any, List
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_PLACEHOLDER_SECRET_MARKERS = ("change_in_production", "replace-with", "changeme", "your-secret", "example")


class Settings(BaseSettings):
    app_name: str = "Nexora Price Intelligence Platform"
    app_version: str = "0.1.0"
    environment: str = "development"
    debug: bool = False

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/nexora"

    @field_validator("database_url", mode="before")
    @classmethod
    def _normalize_async_database_url(cls, value: Any) -> Any:
        """Accept Render's standard Postgres URL while using SQLAlchemy's async driver."""
        if not isinstance(value, str):
            return value
        value = value.strip()
        if value.startswith("postgres://"):
            return "postgresql+asyncpg://" + value[len("postgres://"):]
        if value.startswith("postgresql://"):
            return "postgresql+asyncpg://" + value[len("postgresql://"):]
        return value

    redis_url: str = "redis://localhost:6379/0"

    jwt_secret_key: str
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7
    verification_token_expire_hours: int = 24
    password_reset_token_expire_minutes: int = 15

    rate_limit_enabled: bool = True
    rate_limit_login_per_minute: int = 5
    rate_limit_register_per_hour: int = 10
    rate_limit_forgot_password_per_hour: int = 3
    rate_limit_resend_verification_per_hour: int = 3

    allowed_hosts: List[str] = ["*"]
    cors_origins: List[str] = [
        "http://localhost:3000",
        "http://localhost:5173",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:5173",
    ]

    @model_validator(mode="before")
    @classmethod
    def _parse_list_fields(cls, data: Any) -> Any:
        """Accept comma-separated strings for list fields from env vars."""
        if isinstance(data, dict):
            for key in ("cors_origins", "allowed_hosts"):
                raw = data.get(key)
                if isinstance(raw, str):
                    data[key] = [s.strip() for s in raw.split(",") if s.strip()]
        return data

    @field_validator("cors_origins", "allowed_hosts", mode="before")
    @classmethod
    def _coerce_list(cls, value: Any) -> Any:
        """Accept comma-separated string from env var."""
        if isinstance(value, str):
            return [s.strip() for s in value.split(",") if s.strip()]
        return value

    block_disposable_emails: bool = True

    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from_email: str = ""
    smtp_from_name: str = "Nexora"
    frontend_url: str = "http://localhost:5173"

    email_verification_bypass: bool = False
    allow_production_email_verification_bypass: bool = False

    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.6-flash"
    gemini_discovery_enabled: bool = True

    residential_proxy_url: str = ""
    auto_render_enabled: bool = True

    # Explicitly opt-in test-only allowance for the isolated local HTML fixture server.
    # Never enable this in a deployed or production environment.
    allow_local_test_fixtures: bool = False

    scheduled_collection_interval_minutes: int = 60
    job_lease_seconds: int = 3600

    @model_validator(mode="after")
    def validate_runtime_security(self) -> "Settings":
        secret = self.jwt_secret_key
        lowered = secret.lower()
        if len(secret) < 32 or any(marker in lowered for marker in _PLACEHOLDER_SECRET_MARKERS):
            raise ValueError(
                "JWT_SECRET_KEY must be a random secret of at least 32 characters (placeholder values are rejected). "
                'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        if self.allow_local_test_fixtures and self.environment.lower() in {"prod", "production"}:
            raise ValueError("ALLOW_LOCAL_TEST_FIXTURES cannot be enabled in production.")
        if self.environment.lower() in {"prod", "production"} and "*" in self.allowed_hosts:
            raise ValueError("ALLOWED_HOSTS must be explicit in production.")
        if self.environment.lower() in {"prod", "production"} and not self.database_url.startswith(
            "postgresql+asyncpg://"
        ):
            raise ValueError(
                "DATABASE_URL must point to PostgreSQL in production. "
                "Use the Render Postgres internal connection string."
            )
        if (
            self.email_verification_bypass
            and self.environment.lower() in {"prod", "production"}
            and not self.allow_production_email_verification_bypass
        ):
            raise ValueError(
                "Production email verification bypass requires "
                "ALLOW_PRODUCTION_EMAIL_VERIFICATION_BYPASS=true."
            )
        return self

    @property
    def smtp_configured(self) -> bool:
        """True only when both SMTP username and password are present."""
        return bool(self.smtp_username.strip() and self.smtp_password.strip())

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        enable_decoding=False,
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()

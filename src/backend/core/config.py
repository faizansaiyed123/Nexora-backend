from functools import lru_cache
from typing import List
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Nexora Price Intelligence Platform"
    app_version: str = "0.1.0"
    environment: str = "development"
    debug: bool = False

    # Database
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/nexora"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # JWT Authentication
    jwt_secret_key: str = "nexora_super_secret_jwt_key_change_in_production_32bytes_min"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15  # 15 minutes short-lived
    refresh_token_expire_days: int = 7     # 7 days rotation window
    verification_token_expire_hours: int = 24
    password_reset_token_expire_minutes: int = 15

    # Rate Limiting
    rate_limit_enabled: bool = True
    rate_limit_login_per_minute: int = 5
    rate_limit_register_per_hour: int = 10
    rate_limit_forgot_password_per_hour: int = 3
    rate_limit_resend_verification_per_hour: int = 3

    # Security & CORS
    allowed_hosts: List[str] = ["*"]
    cors_origins: List[str] = ["http://localhost:3000", "http://localhost:5173", "http://127.0.0.1:3000"]

    # Disposable Email Blocking
    block_disposable_emails: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()

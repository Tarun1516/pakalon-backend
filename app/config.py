"""Application configuration via pydantic-settings.

All values are read from environment variables.
Usage:
    from app.config import get_settings
    settings = get_settings()
"""
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


BACKEND_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """Centralised application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=BACKEND_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ───────────────────────────────────────────
    environment: Literal["development", "staging", "production"] = "development"
    log_level: str = "INFO"
    allowed_origins: list[str] = ["http://localhost:3000", "https://pakalon.com"]

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def parse_allowed_origins(cls, v: Any) -> Any:
        """Accept both JSON arrays and comma-separated strings."""
        if isinstance(v, str):
            stripped = v.strip()
            if stripped.startswith("["):
                import json
                return json.loads(stripped)
            return [origin.strip() for origin in stripped.split(",") if origin.strip()]
        return v

    # ── PostgreSQL ────────────────────────────────────────────
    database_url: str = "postgresql+psycopg://pakalon:pakalon@localhost:5432/pakalon"
    development_allow_sqlite_fallback: bool = True
    development_database_fallback_url: str = (
        f"sqlite+aiosqlite:///{(BACKEND_ROOT / '.local' / 'pakalon-dev.db').as_posix()}"
    )

    # ── Redis ─────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"
    development_allow_fakeredis_fallback: bool = True

    # ── Supabase ──────────────────────────────────────────
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_jwt_secret: str = ""
    supabase_webhook_secret: str = ""  # Shared secret for Supabase auth webhooks

    # ── Polar ─────────────────────────────────────────────────
    polar_access_token: str = ""
    polar_webhook_secret: str = ""
    polar_product_id: str = ""
    polar_product_price_id: str = ""

    # ── Resend ────────────────────────────────────────────────
    resend_api_key: str = ""
    resend_from_email: str = "noreply@pakalon.com"
    email_from: str = "Pakalon <noreply@pakalon.com>"

    # ── Frontend ──────────────────────────────────────────────
    frontend_url: str = "https://pakalon.com"
    backend_public_url: str = "http://localhost:8000"

    # ── OAuth Connectors (Automations) ───────────────────────
    github_oauth_client_id: str = ""
    github_oauth_client_secret: str = ""
    slack_oauth_client_id: str = ""
    slack_oauth_client_secret: str = ""
    logo_dev_publishable_key: str = Field(
        default="",
        validation_alias=AliasChoices("LOGO_DEV_PUBLISHABLE_KEY", "PUBLISHABLE_KEY"),
    )
    logo_dev_secret_key: str = Field(
        default="",
        validation_alias=AliasChoices("LOGO_DEV_SECRET_KEY", "SECRET_KEY"),
    )

    # ── OpenRouter ────────────────────────────────────────────
    openrouter_master_key: str = ""

    # ── Security ──────────────────────────────────────────────
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_days: int = 90

    # ── APScheduler ───────────────────────────────────────────
    scheduler_timezone: str = "UTC"

    # ── GeoIP (MaxMind GeoLite2-City) ─────────────────────────
    # Download from: https://dev.maxmind.com/geoip/geolite2-free-geolocation-data
    geoip_db_path: str = ""  # e.g. /etc/geoip/GeoLite2-City.mmdb
    # MaxMind license key — required for auto-update job (T-BE-21).
    # Get one free at https://www.maxmind.com/en/geolite2/signup
    maxmind_license_key: str = ""

    # ── Cloud Storage ─────────────────────────────────────────
    # MinIO (self-hosted S3-compatible)
    minio_endpoint: str = ""            # e.g. minio.example.com:9000
    minio_access_key: str = ""
    minio_secret_key: str = ""
    minio_bucket: str = "pakalon"
    minio_secure: bool = True

    # Cloudinary (optional cloud backup)
    cloudinary_cloud_name: str = ""
    cloudinary_api_key: str = ""
    cloudinary_api_secret: str = ""

    # Primary storage backend: "minio" | "cloudinary" | "local"
    storage_backend: str = "local"
    local_storage_path: str = "/tmp/pakalon_storage"

    # ── Admin ─────────────────────────────────────────────────
    admin_api_key: str = ""  # static key for admin-only endpoints

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_development(self) -> bool:
        return self.environment == "development"


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (singleton)."""
    return Settings()

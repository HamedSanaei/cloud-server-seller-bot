from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"
    database_url: str = "postgresql+asyncpg://cloud:cloud@localhost:5432/cloud"
    redis_url: str = "redis://localhost:6379/0"
    telegram_bot_token: str = ""
    hetzner_api_token: str = ""
    hetzner_api_base_url: str = "https://api.hetzner.cloud/v1"
    arvancloud_api_key: str = ""
    arvancloud_api_base_url: str = "https://napi.arvancloud.ir/ecc/v1"
    arvancloud_region: str = ""
    provider_credential_encryption_key: str = ""
    payment_gateway_secrets: dict[str, str] = Field(default_factory=dict)
    default_currency: str = "EUR"
    customer_billing_quantum_seconds: int = Field(default=3600, ge=60)
    low_balance_threshold_minor: int = Field(default=5000, ge=0)
    low_balance_grace_hours: int = Field(default=24, ge=0)
    backup_output_dir: str = "./backups"
    backup_retention_days: int = Field(default=14, ge=1)
    backup_encryption_key: str = ""
    # OpenTelemetry (M11-002): OTLP/GRPC collector endpoint
    # (e.g. http://otel-collector:4317); empty = in-process spans, no export.
    otel_exporter_endpoint: str = ""
    otel_sample_ratio: float = Field(default=1.0, ge=0.0, le=1.0)
    # Snapshot storage rate card (M13-004): the OPERATOR-declared price of
    # snapshot storage - minor units per GB per month. Never a hardcoded
    # provider price; 0 means snapshots are free in this environment.
    snapshot_rate_currency: str = ""
    snapshot_per_gb_month_minor: int = Field(default=0, ge=0)
    # REST v1 identity (M14-001/M14-002): the x-platform-user header is a
    # DEV/TEST fallback and MUST stay disabled in production, where requests
    # authenticate with revocable hashed bearer tokens.
    api_allow_header_identity: bool = False
    # REST v1 rate limiting (M14-006): per-identity sliding window; each
    # API token (or dev identity) gets its own bucket per API process.
    api_rate_limit_per_minute: int = Field(default=240, ge=1)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

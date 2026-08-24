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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

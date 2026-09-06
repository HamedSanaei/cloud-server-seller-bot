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
    # M08-001: HMAC key for signed Telegram callback buttons. Must be set
    # whenever the bot runs; empty disables callback-driven flows (services
    # reject an empty key at construction time).
    callback_signing_key: str = ""
    hetzner_api_token: str = ""
    hetzner_api_base_url: str = "https://api.hetzner.cloud/v1"
    leaseweb_api_key: str = ""
    leaseweb_api_base_url: str = "https://api.leaseweb.com"
    # LEASEWEB-MVP: ordering-VPS catalog scope (comma-separated allowlist).
    # Only these locations are ever synced or sold.
    leaseweb_locations: str = "AMS-01,FRA-01"
    # Optional comma-separated OS allowlist; empty = every free OS option.
    leaseweb_os_allowlist: str = ""
    # Only OS options that do not change the base monthly price are sellable.
    leaseweb_order_os_only_free: bool = True
    # SAFETY SWITCH: without this explicitly set to true, no code path used
    # by tests/dev verification may place a REAL billable Leaseweb order.
    leaseweb_allow_live_order_test: bool = False
    # Telegram admin alert chat for renewals/attention items (0 = unset).
    telegram_admin_chat_id: int = 0
    # Optional support contact shown on the support screen (e.g. @handle).
    support_contact: str = ""
    arvancloud_api_key: str = ""
    arvancloud_api_base_url: str = "https://napi.arvancloud.ir/ecc/v1"
    arvancloud_region: str = ""
    provider_credential_encryption_key: str = ""
    zarinpal_merchant_id: str = ""
    zarinpal_base_url: str = "https://api.zarinpal.com/pg/v4/payment"
    zarinpal_sandbox: bool = False
    zarinpal_callback_url: str = ""
    payment_gateway_secrets: dict[str, str] = Field(default_factory=dict)
    default_currency: str = "EUR"
    # The OPERATOR-declared price book the selling price is derived from
    # (M08-005): confirmation, holds and immutable server price snapshots
    # all read the SAME book. There is no versioned book by default.
    price_book_name: str = "retail-eur"
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
    # Backups surcharge (M13-005): the OPERATOR-declared percentage of a
    # server's monthly price that backups cost, in basis points (2000 bp
    # = 20%). Never a hardcoded provider default; 0 means free.
    backup_surcharge_bps: int = Field(default=0, ge=0, le=10_000)
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

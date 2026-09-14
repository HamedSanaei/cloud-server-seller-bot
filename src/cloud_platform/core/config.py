"""Application configuration — ``configuration.toml`` is the source of truth.

Runtime configuration lives in ONE operator-managed TOML file. Environment
variables remain supported for two narrow purposes only:

1. **bootstrap** — ``CLOUD_PLATFORM_CONFIG_FILE`` tells the process *where*
   the file is (it must never contain a secret itself);
2. **test/CI compatibility** — the existing ``.env``/environment contract is
   preserved so the test suite and local development keep working unchanged.

Precedence (highest wins)::

    explicit constructor kwargs
      > environment variables            # bootstrap / tests
      > configuration.toml               # the runtime configuration
      > .env                             # local development convenience
      > field defaults

Production containers therefore mount the TOML file and set no application
secrets in the environment. The file is looked up at, in order:

1. the path passed to :func:`load_settings` (tests),
2. ``$CLOUD_PLATFORM_CONFIG_FILE`` (must exist when set),
3. ``./configuration.toml`` (development),
4. ``/etc/cloud-server-seller/configuration.toml`` (production default).

``configuration.example.toml`` (committed) documents every supported section
with safe fake values; the real ``configuration.toml`` is git-ignored.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

#: Bootstrap variable naming the configuration file. Never a secret.
CONFIG_FILE_ENV = "CLOUD_PLATFORM_CONFIG_FILE"

#: Production default, then the development default.
DEFAULT_CONFIG_FILE = "/etc/cloud-server-seller/configuration.toml"
DEV_CONFIG_FILE = "configuration.toml"

#: Market of each provider when the TOML does not say (LEASEWEB-MVP + Iran
#: storefront). Keys are provider keys; values are ``iran`` or ``foreign``.
DEFAULT_PROVIDER_MARKETS: dict[str, str] = {
    "leaseweb": "foreign",
    "hetzner": "foreign",
    "arvancloud": "iran",
}

#: Customer-facing provider names (never internal keys) for the storefront.
DEFAULT_PROVIDER_DISPLAY_NAMES: dict[str, str] = {
    "leaseweb": "Leaseweb",
    "hetzner": "Hetzner",
    "arvancloud": "ArvanCloud",
}


class ConfigFileError(ValueError):
    """The configured TOML file is missing or unreadable."""


# ---------------------------------------------------------------------------
# TOML -> flat Settings mapping
# ---------------------------------------------------------------------------

#: ``[section.subsection] key`` -> ``Settings`` field name.
_TOML_FIELDS: Mapping[tuple[str, ...], str] = {
    ("app", "environment"): "app_env",
    ("app", "log_level"): "log_level",
    ("app", "default_currency"): "default_currency",
    ("database", "url"): "database_url",
    ("redis", "url"): "redis_url",
    ("telegram", "bot_token"): "telegram_bot_token",
    ("telegram", "callback_signing_key"): "callback_signing_key",
    ("telegram", "admin_chat_id"): "telegram_admin_chat_id",
    ("telegram", "support_contact"): "support_contact",
    ("telegram", "logger", "enabled"): "telegram_logger_enabled",
    ("telegram", "logger", "chat_id"): "telegram_logger_chat_id",
    ("telegram", "logger", "log_purchases"): "telegram_logger_log_purchases",
    ("telegram", "logger", "log_recharges"): "telegram_logger_log_recharges",
    ("telegram", "logger", "log_payment_failures"): "telegram_logger_log_payment_failures",
    ("telegram", "logger", "log_order_failures"): "telegram_logger_log_order_failures",
    ("telegram", "logger", "log_admin_wallet_adjustments"): (
        "telegram_logger_log_admin_wallet_adjustments"
    ),
    ("payments", "zarinpal", "enabled"): "zarinpal_enabled",
    ("payments", "zarinpal", "merchant_id"): "zarinpal_merchant_id",
    ("payments", "zarinpal", "base_url"): "zarinpal_base_url",
    ("payments", "zarinpal", "sandbox"): "zarinpal_sandbox",
    ("payments", "zarinpal", "callback_url"): "zarinpal_callback_url",
    ("security", "provider_credential_encryption_key"): ("provider_credential_encryption_key"),
    ("security", "backup_encryption_key"): "backup_encryption_key",
    ("billing", "price_book_name"): "price_book_name",
    ("billing", "customer_billing_quantum_seconds"): ("customer_billing_quantum_seconds"),
    ("billing", "low_balance_threshold_minor"): "low_balance_threshold_minor",
    ("billing", "low_balance_grace_hours"): "low_balance_grace_hours",
    ("backup", "output_dir"): "backup_output_dir",
    ("backup", "retention_days"): "backup_retention_days",
    ("observability", "otel_exporter_endpoint"): "otel_exporter_endpoint",
    ("observability", "otel_sample_ratio"): "otel_sample_ratio",
    ("api", "allow_header_identity"): "api_allow_header_identity",
    ("api", "rate_limit_per_minute"): "api_rate_limit_per_minute",
    # --- Customer-facing server management (Telegram My Servers) ----------
    # Which customer capabilities the deployment exposes. Defaults live on
    # ``Settings``/``ServerManagementPolicy`` and are conservative for
    # destructive or rarely self-serviced operations.
    ("features", "server_management", "enabled"): "server_management_enabled",
    ("features", "server_management", "manager"): "server_management_manager",
    ("features", "server_management", "power"): "server_management_power",
    ("features", "server_management", "console"): "server_management_console",
    ("features", "server_management", "traffic"): "server_management_traffic",
    ("features", "server_management", "snapshots"): "server_management_snapshots",
    ("features", "server_management", "reinstall"): "server_management_reinstall",
    ("features", "server_management", "password_reset"): ("server_management_password_reset"),
    ("features", "server_management", "iso"): "server_management_iso",
    ("features", "server_management", "ip_management"): ("server_management_ip_management"),
    ("features", "server_management", "monitoring"): "server_management_monitoring",
    ("features", "server_management", "billing"): "server_management_billing",
    ("features", "server_management", "page_size"): "server_management_page_size",
    ("features", "server_management", "traffic_window_days"): (
        "server_management_traffic_window_days"
    ),
    ("features", "server_management", "confirmation_ttl_seconds"): (
        "server_management_confirmation_ttl_seconds"
    ),
    # --- Telegram transient state (PROD-HARDENING) ------------------------
    # Where callback references, pending confirmations and prompts live. The
    # state is disposable, so it is allowed to expire — but it must be shared,
    # because a restart or a second replica would otherwise lose the buttons
    # the customer is holding.
    ("telegram", "sessions", "backend"): "telegram_sessions_backend",
    ("telegram", "sessions", "namespace"): "telegram_sessions_namespace",
    ("telegram", "sessions", "reference_ttl_seconds"): ("telegram_sessions_reference_ttl_seconds"),
    ("telegram", "sessions", "prompt_ttl_seconds"): "telegram_sessions_prompt_ttl_seconds",
    ("telegram", "sessions", "confirmation_ttl_seconds"): (
        "telegram_sessions_confirmation_ttl_seconds"
    ),
    ("telegram", "sessions", "replay_ttl_seconds"): "telegram_sessions_replay_ttl_seconds",
    # --- Commercial service lifecycle ------------------------------------
    # The CUSTOMER billing lifecycle is deliberately separate from the
    # provider's infrastructure contract: these settings never make a provider
    # API call, they only drive local wallet collection and notifications.
    ("commerce", "renewal", "enabled"): "commerce_renewal_enabled",
    ("commerce", "renewal", "charge_before_expiry_hours"): (
        "commerce_renewal_charge_before_expiry_hours"
    ),
    ("commerce", "renewal", "warning_before_expiry_hours"): (
        "commerce_renewal_warning_before_expiry_hours"
    ),
    ("commerce", "renewal", "grace_period_hours"): "commerce_renewal_grace_period_hours",
    ("commerce", "renewal", "auto_renew_default"): "commerce_renewal_auto_renew_default",
    ("commerce", "suspension", "stop_server_after_grace"): (
        "commerce_suspension_stop_server_after_grace"
    ),
}

#: TOML keys that are lists in the file but a comma-separated ``Settings``
#: string (so one flat field covers both the TOML and the environment form).
_SEQUENCE_FIELDS: frozenset[str] = frozenset(
    {
        "commerce_renewal_warning_before_expiry_hours",
    }
)

#: ``[providers.<key>]`` sub-keys -> ``Settings`` field name. Keys absent from
#: this table (``enabled``, ``market``, ``display_name``) are handled
#: generically per provider.
_PROVIDER_FIELDS: Mapping[str, Mapping[str, str]] = {
    "leaseweb": {
        "api_key": "leaseweb_api_key",
        "base_url": "leaseweb_api_base_url",
        "timeout_seconds": "leaseweb_timeout_seconds",
        "locations": "leaseweb_locations",
        "os_allowlist": "leaseweb_os_allowlist",
        "order_os_only_free": "leaseweb_order_os_only_free",
        "contract_term": "leaseweb_contract_term",
        "billing_cycle": "leaseweb_billing_cycle",
    },
    "hetzner": {
        "api_token": "hetzner_api_token",
        "api_key": "hetzner_api_token",
        "base_url": "hetzner_api_base_url",
    },
    "arvancloud": {
        "api_key": "arvancloud_api_key",
        "base_url": "arvancloud_api_base_url",
        "region": "arvancloud_region",
    },
}

#: Optional NESTED provider subsections -> Settings field name, e.g.
#: ``[providers.leaseweb.ordering] contract_term = "1_MONTH"``. The flat
#: ``[providers.leaseweb]`` keys stay canonical (ADR-014 §3); a nested value
#: is more specific and therefore wins when both are present.
_PROVIDER_NESTED_FIELDS: Mapping[tuple[str, str], Mapping[str, str]] = {
    ("leaseweb", "ordering"): {
        "locations": "leaseweb_locations",
        "os_allowlist": "leaseweb_os_allowlist",
        "only_free_os": "leaseweb_order_os_only_free",
        "order_os_only_free": "leaseweb_order_os_only_free",
        "contract_term": "leaseweb_contract_term",
        "billing_cycle": "leaseweb_billing_cycle",
    },
    ("leaseweb", "transport"): {
        "base_url": "leaseweb_api_base_url",
        "api_key": "leaseweb_api_key",
        "timeout_seconds": "leaseweb_timeout_seconds",
    },
}

#: List-valued TOML keys serialized into the comma-separated Settings string.
_PROVIDER_SEQUENCE_FIELDS: frozenset[str] = frozenset({"locations", "os_allowlist"})


def _dig(data: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    """Read a nested path, returning None when any level is missing."""
    node: Any = data
    for part in path:
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _provider_sections(data: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    providers = data.get("providers")
    if not isinstance(providers, Mapping):
        return {}
    return {str(key): dict(value) for key, value in providers.items() if isinstance(value, Mapping)}


def toml_to_settings(data: Mapping[str, Any]) -> dict[str, Any]:
    """Map a parsed TOML document onto flat ``Settings`` field values.

    Unknown keys are ignored (forward compatibility); the result is a plain
    mapping that the TOML settings source hands to pydantic.
    """
    values: dict[str, Any] = {}
    for path, field in _TOML_FIELDS.items():
        raw = _dig(data, path)
        if raw is None:
            continue
        if field in _SEQUENCE_FIELDS and isinstance(raw, list | tuple):
            values[field] = ",".join(str(part).strip() for part in raw if str(part).strip())
        else:
            values[field] = raw

    markets: dict[str, str] = {}
    display_names: dict[str, str] = {}
    enabled: dict[str, bool] = {}
    for provider_key, section in _provider_sections(data).items():
        for sub_key, field in _PROVIDER_FIELDS.get(provider_key, {}).items():
            if sub_key not in section:
                continue
            raw = section[sub_key]
            if sub_key in _PROVIDER_SEQUENCE_FIELDS:
                if isinstance(raw, str):
                    values[field] = raw
                elif isinstance(raw, list | tuple):
                    values[field] = ",".join(str(part).strip() for part in raw if str(part).strip())
            else:
                values[field] = raw
        if "enabled" in section:
            enabled[provider_key] = bool(section["enabled"])
        if "market" in section:
            markets[provider_key] = str(section["market"]).strip().lower()
        if "display_name" in section:
            display_names[provider_key] = str(section["display_name"])

    # Optional nested subsections, e.g. ``[providers.leaseweb.ordering]``.
    for (provider_key, section_key), mapping in _PROVIDER_NESTED_FIELDS.items():
        nested = _dig(data, ("providers", provider_key, section_key))
        if not isinstance(nested, Mapping):
            continue
        for sub_key, field in mapping.items():
            if sub_key not in nested:
                continue
            raw = nested[sub_key]
            if sub_key in _PROVIDER_SEQUENCE_FIELDS:
                if isinstance(raw, str):
                    values[field] = raw
                elif isinstance(raw, list | tuple):
                    values[field] = ",".join(str(part).strip() for part in raw if str(part).strip())
            else:
                values[field] = raw

    if enabled:
        values["providers_enabled"] = enabled
    if markets:
        values["provider_markets"] = markets
    if display_names:
        values["provider_display_names"] = display_names
    return values


def parse_config_file(path: str | Path) -> dict[str, Any]:
    """Parse ``path`` and map it onto flat Settings values."""
    file_path = Path(path)
    try:
        with file_path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigFileError(f"configuration file not found: {file_path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigFileError(f"configuration file {file_path} is not valid TOML: {exc}") from exc
    return toml_to_settings(data)


#: Configuration file selected for the next Settings construction, used by
#: :meth:`Settings.model_validate_toml` so an explicit path keeps the normal
#: precedence (init > environment > TOML > .env).
_ACTIVE_CONFIG_FILE: Path | None = None


def resolve_config_file(explicit: str | Path | None = None) -> Path | None:
    """Resolve the configuration file path (see module docstring).

    An explicitly configured path (argument or ``CLOUD_PLATFORM_CONFIG_FILE``)
    must exist — silently running with defaults because of a typo would be a
    production incident. Default candidates are optional.
    """
    for candidate in (explicit, _ACTIVE_CONFIG_FILE, os.environ.get(CONFIG_FILE_ENV)):
        if candidate:
            path = Path(candidate)
            if not path.is_file():
                raise ConfigFileError(f"configuration file not found: {path}")
            return path
    for default in (DEV_CONFIG_FILE, DEFAULT_CONFIG_FILE):
        path = Path(default)
        if path.is_file():
            return path
    return None


class _TomlSettingsSource(PydanticBaseSettingsSource):
    """pydantic-settings source backed by the mapped TOML document."""

    def __init__(self, settings_cls: type[BaseSettings], data: Mapping[str, Any]) -> None:
        super().__init__(settings_cls)
        self._data = dict(data)

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict[str, Any]:
        known = set(self.settings_cls.model_fields)
        return {key: value for key, value in self._data.items() if key in known}


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
    # Connect/read/write/pool timeout for every Leaseweb request (the ONE
    # shared transport owns it; nothing else in the codebase sets a timeout).
    leaseweb_timeout_seconds: float = Field(default=30.0, gt=0)
    # LEASEWEB-MVP: ordering-VPS catalog scope (comma-separated allowlist).
    # Only these locations are ever synced or sold.
    leaseweb_locations: str = "AMS-01,FRA-01"
    # Optional comma-separated OS allowlist; empty = every free OS option.
    leaseweb_os_allowlist: str = ""
    # Only OS options that do not change the base monthly price are sellable.
    leaseweb_order_os_only_free: bool = True
    # Contract term / billing cycle used for every order (snapshotted on the
    # provider-order row at checkout, before any provider call).
    leaseweb_contract_term: str = "1_MONTH"
    leaseweb_billing_cycle: str = "1_MONTH"
    # Telegram admin alert chat for renewals/attention items (0 = unset).
    telegram_admin_chat_id: int = 0
    # Optional support contact shown on the support screen (e.g. @handle).
    support_contact: str = ""
    # --- Private Telegram business logger channel -------------------------
    # A dedicated channel where the operator follows business events. It is
    # delivered through the durable business-log outbox, never inline, so a
    # Telegram outage can never affect checkout/payment/reconciliation.
    telegram_logger_enabled: bool = False
    telegram_logger_chat_id: int = 0
    telegram_logger_log_purchases: bool = True
    telegram_logger_log_recharges: bool = True
    telegram_logger_log_payment_failures: bool = True
    telegram_logger_log_order_failures: bool = True
    telegram_logger_log_admin_wallet_adjustments: bool = True
    # --- Storefront market metadata (provider-neutral) ---------------------
    # market of each provider (`iran` / `foreign`), its customer-facing name
    # and whether the operator lists it. Domain code never branches on a
    # concrete provider name — it reads these maps.
    provider_markets: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_PROVIDER_MARKETS))
    provider_display_names: dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_PROVIDER_DISPLAY_NAMES)
    )
    providers_enabled: dict[str, bool] = Field(default_factory=dict)
    arvancloud_api_key: str = ""
    arvancloud_api_base_url: str = "https://napi.arvancloud.ir/ecc/v1"
    arvancloud_region: str = ""
    provider_credential_encryption_key: str = ""
    zarinpal_enabled: bool = True
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
    # --- Customer-facing server management (`[features.server_management]`) --
    # The operator decides which VPS capabilities paying customers may use.
    # Every default here mirrors
    # ``cloud_platform.modules.servers.policies.ServerManagementPolicy``: the
    # fields are read with ``getattr`` so the two can never drift, and ISO
    # attach/detach stays OFF by default because it changes the boot medium of
    # a live server and is rarely a self-service need.
    server_management_enabled: bool = True
    server_management_manager: bool = True
    server_management_power: bool = True
    server_management_console: bool = True
    server_management_traffic: bool = True
    server_management_snapshots: bool = True
    server_management_reinstall: bool = True
    server_management_password_reset: bool = True
    server_management_iso: bool = False
    server_management_ip_management: bool = True
    server_management_monitoring: bool = True
    # Commercial self-service: "renew now" (one wallet-settled period) and the
    # durable auto-renew toggle. Neither calls a provider API; both are
    # ownership-checked, and renew-now additionally needs a one-time
    # confirmation because it moves money.
    server_management_billing: bool = True
    # Telegram page size for the server list (a screen must fit one message).
    server_management_page_size: int = Field(default=5, ge=1, le=20)
    # Trailing window the traffic screen requests from the provider.
    server_management_traffic_window_days: int = Field(default=30, ge=1, le=365)
    # Lifetime of a one-time destructive-operation confirmation token.
    server_management_confirmation_ttl_seconds: int = Field(default=900, ge=30, le=3600)

    # --- Telegram transient state (`[telegram.sessions]`) ------------------
    # `redis` is the only production-safe backend: callback references, pending
    # confirmations and prompts must survive a restart and be visible to every
    # replica. `memory` exists for tests and an explicitly configured
    # development environment only (the factory refuses it elsewhere).
    telegram_sessions_backend: str = "memory"
    telegram_sessions_namespace: str = "cloud-platform:bot"
    # How long a server reference / remembered selection stays resolvable.
    telegram_sessions_reference_ttl_seconds: int = Field(default=1800, ge=30, le=86_400)
    # How long a free-text prompt (server name, reverse DNS) waits for an answer.
    telegram_sessions_prompt_ttl_seconds: int = Field(default=900, ge=30, le=86_400)
    # How long an issued destructive-operation confirmation stays valid.
    telegram_sessions_confirmation_ttl_seconds: int = Field(default=300, ge=30, le=3600)
    # How long a consumed confirmation is remembered, so a replay is answered
    # with "already processed" instead of being silently swallowed.
    telegram_sessions_replay_ttl_seconds: int = Field(default=1800, ge=30, le=86_400)

    # --- Commercial service lifecycle (`[commerce.*]`) ---------------------
    # Separate from provider infrastructure state: these settings drive the
    # customer wallet collection, warnings and grace/suspension policy only.
    commerce_renewal_enabled: bool = True
    # Hours before expiry the worker may attempt an automatic wallet charge.
    commerce_renewal_charge_before_expiry_hours: int = Field(default=72, ge=0, le=720)
    # Warning thresholds (hours before expiry), most distant first.
    commerce_renewal_warning_before_expiry_hours: str = "168,72,24"
    # After expiry, how long the service stays payable before suspension.
    commerce_renewal_grace_period_hours: int = Field(default=48, ge=0, le=720)
    # Default for a newly provisioned service's automatic renewal.
    commerce_renewal_auto_renew_default: bool = True
    # Suspension must never silently stop a provider server: this stays off
    # unless the operator deliberately opts in.
    commerce_suspension_stop_server_after_grace: bool = False

    #: Flat values read from ``configuration.toml`` (introspection/tests).
    toml_values: dict[str, Any] = Field(default_factory=dict, exclude=True)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Precedence: init > environment > TOML > .env > secrets.

        A bare ``Settings()`` deliberately does NOT consult the filesystem: a
        developer's local ``configuration.toml`` must never leak into a unit
        test that asserts pure defaults. The file is read only when the
        caller went through :func:`load_settings` / :func:`get_settings` (or
        :meth:`model_validate_toml`), which select ``_ACTIVE_CONFIG_FILE``
        first.
        """
        del file_secret_settings  # unused: no secrets-directory source
        path = _ACTIVE_CONFIG_FILE
        if path is None:
            return (init_settings, env_settings, dotenv_settings)
        data = parse_config_file(path)
        if not data:  # pragma: no cover - empty file keeps env/defaults only
            return (init_settings, env_settings, dotenv_settings)
        return (
            init_settings,
            env_settings,
            _TomlSettingsSource(settings_cls, data),
            dotenv_settings,
        )

    @classmethod
    def model_validate_toml(cls, path: str | Path | None = None) -> Settings:
        """Build settings from a specific TOML file (tests/CLI).

        The path becomes the active configuration file for this construction
        only; environment variables keep their documented precedence.
        """
        global _ACTIVE_CONFIG_FILE
        resolved = resolve_config_file(path)
        previous = _ACTIVE_CONFIG_FILE
        _ACTIVE_CONFIG_FILE = resolved
        try:
            settings = cls()
            settings.toml_values = parse_config_file(resolved) if resolved is not None else {}
            return settings
        finally:
            _ACTIVE_CONFIG_FILE = previous


def load_settings(config_file: str | Path | None = None) -> Settings:
    """Load settings, optionally from an explicit configuration file."""
    return Settings.model_validate_toml(config_file)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process-wide settings singleton (TOML-first; see module docstring)."""
    return load_settings()


def reset_settings_cache() -> None:
    """Drop the cached settings (tests that swap the configuration file)."""
    get_settings.cache_clear()

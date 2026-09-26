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

import math
import os
import re
import tomllib
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator
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
    ("payments", "tetraminator", "enabled"): "tetraminator_enabled",
    ("payments", "tetraminator", "api_key"): "tetraminator_api_key",
    ("payments", "tetraminator", "base_url"): "tetraminator_base_url",
    ("payments", "tetraminator", "callback_url"): "tetraminator_callback_url",
    ("payments", "tetraminator", "timeout_seconds"): "tetraminator_timeout_seconds",
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
    # --- Currency / FX resolution (platform-level, provider-neutral) --------
    # The FIRST live source is AbanTether's public ticker (read-only, no key).
    ("fx", "enabled"): "fx_enabled",
    ("fx", "domestic_enabled"): "fx_domestic_enabled",
    ("fx", "global_enabled"): "fx_global_enabled",
    ("fx", "provider"): "fx_provider",  # backward-compatible legacy name
    ("fx", "domestic_provider"): "fx_domestic_provider",
    ("fx", "default_display_currency"): "fx_default_display_currency",
    ("fx", "catalog_pricing_currency"): "fx_catalog_pricing_currency",
    ("fx", "global_fiat_provider"): "fx_global_fiat_provider",
    ("fx", "quote_ttl_seconds"): "fx_quote_ttl_seconds",
    ("fx", "max_stale_seconds"): "fx_max_stale_seconds",
    ("fx", "charge_max_stale_seconds"): "fx_charge_max_stale_seconds",
    ("fx", "request_timeout_seconds"): "fx_request_timeout_seconds",
    ("fx", "allow_usdt_proxy_for_display"): "fx_allow_usdt_proxy_for_display",
    ("fx", "allow_usdt_proxy_for_settlement"): "fx_allow_usdt_proxy_for_settlement",
    ("fx", "abantether", "base_url"): "fx_abantether_base_url",
    ("fx", "abantether", "eur_symbol"): "fx_abantether_eur_symbol",
    ("fx", "abantether", "usd_proxy_symbol"): "fx_abantether_usd_proxy_symbol",
    ("fx", "frankfurter", "base_url"): "fx_frankfurter_base_url",
    ("fx", "frankfurter", "request_timeout_seconds"): ("fx_frankfurter_request_timeout_seconds"),
    ("fx", "frankfurter", "quote_ttl_seconds"): "fx_frankfurter_quote_ttl_seconds",
    ("fx", "frankfurter", "max_stale_seconds"): "fx_frankfurter_max_stale_seconds",
    ("fx", "frankfurter", "catalog_max_stale_seconds"): (
        "fx_frankfurter_catalog_max_stale_seconds"
    ),
    # --- Automatic catalog sync (the periodic offer-sync coordinator) -------
    ("storefront", "catalog_sync", "enabled"): "storefront_catalog_sync_enabled",
    ("storefront", "catalog_sync", "interval_seconds"): (
        "storefront_catalog_sync_interval_seconds"
    ),
    ("storefront", "catalog_sync", "timeout_seconds"): ("storefront_catalog_sync_timeout_seconds"),
    ("storefront", "catalog_sync", "fx_safety_margin_seconds"): (
        "storefront_catalog_fx_safety_margin_seconds"
    ),
}

#: Contract keys that are deliberately NOT part of the canonical template:
#: legacy aliases kept only so an old server file keeps loading. The canonical
#: spelling of the same setting is documented in ``configuration.example.toml``
#: instead, and ``config doctor`` never reports these as missing.
TOML_LEGACY_ALIAS_KEYS: frozenset[tuple[str, ...]] = frozenset({("fx", "provider")})

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
        "cloud_account_limit_ttl_seconds": "leaseweb_cloud_account_limit_ttl_seconds",
        # Automatic capacity recovery (LEASEWEB-MULTIACCOUNT). The backoff
        # schedule is a real list of integers; it is passed through (never
        # comma-joined) and validated at the boundary.
        "cloud_capacity_recovery_enabled": "leaseweb_cloud_capacity_recovery_enabled",
        "cloud_capacity_recovery_interval_seconds": (
            "leaseweb_cloud_capacity_recovery_interval_seconds"
        ),
        "cloud_capacity_recovery_backoff_seconds": (
            "leaseweb_cloud_capacity_recovery_backoff_seconds"
        ),
        "cloud_capacity_canary_lease_seconds": ("leaseweb_cloud_capacity_canary_lease_seconds"),
        "cloud_capacity_outage_reminder_delay_seconds": (
            "leaseweb_cloud_capacity_outage_reminder_delay_seconds"
        ),
        "cloud_capacity_outage_reminder_interval_seconds": (
            "leaseweb_cloud_capacity_outage_reminder_interval_seconds"
        ),
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

#: The account id the deprecated single-``api_key`` form maps onto. Mirrors
#: ``cloud_platform.providers.routing.DEFAULT_CREDENTIAL_ACCOUNT``; the two are
#: asserted equal by the test suite so configuration stays import-free of
#: provider code.
DEFAULT_CREDENTIAL_ACCOUNT_ID = "default"

#: Account ids are STABLE, non-secret handles used to pin orders, servers and
#: routing rows. They appear in logs, diagnostics and CLI output, so they are
#: restricted to a safe character set; the API key never is.
_ACCOUNT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")

#: Credential-account lifecycle (mirrors ``providers.routing`` without making
#: this configuration module import provider code).
_ACCOUNT_STATES: frozenset[str] = frozenset({"active", "draining", "disabled"})


class LeasewebAccountSettings(BaseModel):
    """One Leaseweb credential account (LEASEWEB-MULTIACCOUNT).

    This is OUR infrastructure credential — one API key / Sales Organization —
    NOT a customer's provider account (that is
    :class:`cloud_platform.modules.provider_accounts.domain.ProviderAccount`,
    which links an end customer to a provider).

    The operator adds accounts in the server-owned ``configuration.toml``. Both
    shapes are accepted and mean exactly the same thing::

        # Keyed table — the account id IS the table key (no id to keep in sync)
        [providers.leaseweb.accounts.fra-account]
        api_key = "..."
        enabled = true
        label = "Frankfurt Sales Organization"

        # List table — the id is a field
        [[providers.leaseweb.accounts]]
        id = "lw-eu"
        enabled = true
        api_key = "..."
        priority = 100

    One credential may legitimately see SEVERAL locations; locations are never
    configured per account. Each credential discovers its own eligible
    locations from live, read-only provider responses (see
    ``providers/leaseweb/ordering_sync.py``). The ``state`` field separates "may
    receive new orders" from "may still manage what it already owns", so
    draining an account never strands the customer servers it provisioned.

    ``label`` is optional operator-facing text used by ``leaseweb accounts
    doctor`` and never reaches a customer; ``label`` falls back to ``id``.

    ``api_key`` is never rendered: ``repr``/``str``/``model_dump`` used by logs
    or diagnostics show only the stable account id and a length hint.
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    api_key: str = ""
    enabled: bool = True
    priority: int = Field(default=100, ge=0, le=1_000_000)
    state: str = "active"
    #: Optional human label for operators (never customer-visible).
    label: str = ""

    def __repr__(self) -> str:
        return (
            f"LeasewebAccountSettings(id={self.id!r}, enabled={self.enabled!r}, "
            f"priority={self.priority!r}, state={self.state!r}, "
            f"api_key=<redacted len={len(self.api_key)}>)"
        )

    @property
    def display_name(self) -> str:
        """Operator-facing name: the label when set, else the stable id."""
        return (self.label or "").strip() or self.id

    __str__ = __repr__

    @property
    def normalized_state(self) -> str:
        """The canonical lifecycle state (lower-cased, validated)."""
        return (self.state or "active").strip().lower() or "active"

    @property
    def accepts_new_orders(self) -> bool:
        """Whether this account may receive NEW billable business."""
        return self.enabled and self.normalized_state == "active"

    @property
    def usable(self) -> bool:
        """Whether this account may still address what it already owns."""
        return self.normalized_state != "disabled"

    @property
    def has_credential(self) -> bool:
        """Whether a non-blank API key is configured for this account."""
        return bool(self.api_key.strip())


def _dig(data: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    """Read a nested path, returning None when any level is missing."""
    node: Any = data
    for part in path:
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _leaseweb_accounts_toml(data: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    """Read the Leaseweb credential accounts from every accepted TOML shape.

    Shapes (all equivalent, see :class:`LeasewebAccountSettings`):

    1. ``[[providers.leaseweb.accounts]]`` — array of tables with an ``id``;
    2. ``[providers.leaseweb.accounts.<id>]`` — keyed table, the canonical
       form: the account id is the table key, so nothing has to be repeated;
    3. ``[leaseweb.accounts.<id>]`` — the same keyed table at the top level.

    Returns ``None`` when no account is configured from TOML (the deprecated
    single ``api_key`` then owns the provider, as before).

    A keyed entry derives its ``id`` from the table key. Stating an ``id`` that
    DISAGREES with the key is a configuration error and fails closed: the
    account id addresses every server and order that account ever created, so a
    silent rename would orphan them.
    """
    entries: list[dict[str, Any]] = []
    entries.extend(_leaseweb_account_entries(_dig(data, ("providers", "leaseweb", "accounts"))))
    # Convenience alias for the keyed form at the document root.
    entries.extend(_leaseweb_account_entries(_dig(data, ("leaseweb", "accounts"))))
    return entries or None


def _leaseweb_account_entries(raw: Any) -> list[dict[str, Any]]:
    """Normalize one ``accounts`` node (list table or keyed table) to entries."""
    if isinstance(raw, list | tuple):
        return [
            {str(key): value for key, value in dict(entry).items()}
            for entry in raw
            if isinstance(entry, Mapping)
        ]
    if not isinstance(raw, Mapping):
        return []
    entries: list[dict[str, Any]] = []
    for raw_key, value in raw.items():
        if not isinstance(value, Mapping):
            continue
        key = str(raw_key).strip()
        entry = {str(field): field_value for field, field_value in dict(value).items()}
        declared = str(entry.get("id") or "").strip()
        if declared and declared != key:
            raise ValueError(
                f"leaseweb account table {key!r} declares id {declared!r}; the "
                "table key is the account id — remove the id field or rename "
                "the table (the id addresses existing servers and orders)"
            )
        entry["id"] = key
        entries.append(entry)
    return entries


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

    accounts = _leaseweb_accounts_toml(data)
    if accounts is not None:
        values["leaseweb_accounts"] = accounts

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

    # --- Per-provider automatic pricing/publication -------------------------
    # ``[storefront.pricing.<provider>]`` sections (any provider key):
    # ``mode`` (only "markup" is supported), ``markup_percent`` (integer,
    # required for auto-pricing) and ``auto_publish`` (default true). Stored
    # raw; validated where the policy is applied so a typo fails closed with
    # a warning instead of mispricing the storefront.
    storefront = data.get("storefront")
    if isinstance(storefront, Mapping):
        raw_pricing = storefront.get("pricing")
        if isinstance(raw_pricing, Mapping):
            pricing: dict[str, dict[str, Any]] = {}
            for provider_key, section in raw_pricing.items():
                if isinstance(section, Mapping):
                    pricing[str(provider_key)] = {
                        str(key): value for key, value in dict(section).items()
                    }
            if pricing:
                values["storefront_pricing"] = pricing

    # --- Commercial product families --------------------------------------
    # ``[providers.<key>.families.<family>]`` sections (any provider key):
    # ``billing_model`` + ``display_name`` per family. Stored raw; the
    # market catalog validates billing models when it is built.
    families: dict[str, dict[str, dict[str, str]]] = {}
    for provider_key, section in _provider_sections(data).items():
        raw_families = section.get("families")
        if not isinstance(raw_families, Mapping):
            continue
        parsed: dict[str, dict[str, str]] = {}
        for family_key, attrs in raw_families.items():
            if isinstance(attrs, Mapping):
                parsed[str(family_key)] = {
                    str(name): str(value) for name, value in dict(attrs).items()
                }
        if parsed:
            families[str(provider_key)] = parsed
    if families:
        values["provider_families"] = families

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
    #: DEPRECATED single-credential form. Still supported: it is normalized
    #: into one credential account with :data:`DEFAULT_CREDENTIAL_ACCOUNT` as
    #: its id, so existing deployments (and rows they already created) keep
    #: working unchanged. New deployments should use ``accounts``.
    leaseweb_api_key: str = ""
    #: Multiple Leaseweb credential accounts (LEASEWEB-MULTIACCOUNT). One
    #: logical provider key (``leaseweb``) is served by many API keys, each
    #: with its own location scope. Empty => the deprecated single ``api_key``
    #: above (or nothing at all) is used.
    leaseweb_accounts: list[LeasewebAccountSettings] = Field(default_factory=list)
    leaseweb_api_base_url: str = "https://api.leaseweb.com"
    # Connect/read/write/pool timeout for every Leaseweb request (the ONE
    # shared transport owns it; nothing else in the codebase sets a timeout).
    leaseweb_timeout_seconds: float = Field(default=30.0, gt=0)
    # LEASEWEB-MVP: ordering-VPS discovery seeds (comma-separated).
    # These are HINTS ONLY ("locations worth probing"), never an
    # authorization allowlist: every candidate still has to pass its own
    # live eligibility probe, and locations discovered elsewhere (provider
    # payloads, persisted state, built-in datacenter seeds) are probed too.
    # Empty is valid — discovery then relies on the other sources.
    leaseweb_locations: str = "AMS-01,FRA-01"
    # Optional comma-separated OS allowlist; empty = every free OS option.
    leaseweb_os_allowlist: str = ""
    # Only OS options that do not change the base monthly price are sellable.
    leaseweb_order_os_only_free: bool = True
    # Contract term / billing cycle used for every order (snapshotted on the
    # provider-order row at checkout, before any provider call).
    leaseweb_contract_term: str = "1_MONTH"
    leaseweb_billing_cycle: str = "1_MONTH"
    # Hourly cloud: how long ONE definitive provider account-capacity refusal
    # (Leaseweb ``PC-2031`` "Customer limit reached") keeps that credential out
    # of NEW-order publication. The provider publishes no quota endpoint, so
    # capacity can only be learned from a refusal; a bounded window means a
    # single refusal can never disable an account permanently, and the ordinary
    # catalog sync re-probes automatically once it expires. Existing servers,
    # orders and reconciliation are never affected. Minimum 60 seconds.
    leaseweb_cloud_account_limit_ttl_seconds: int = Field(default=3600, ge=60)
    # --- Automatic capacity recovery (LEASEWEB-MULTIACCOUNT) ----------------
    # PC-2031 no longer needs the operator to remember an override clear. A
    # read-only controller inspects the account's instance inventory (and the
    # local delete events), then lets EXACTLY ONE real customer order act as a
    # canary; its provider verdict is what restores eligibility. No synthetic
    # or billable probe server is ever created.
    leaseweb_cloud_capacity_recovery_enabled: bool = True
    #: Cron cadence (minutes) of the read-only recovery controller.
    leaseweb_cloud_capacity_recovery_interval_seconds: int = Field(default=180, ge=60)
    #: Exponential backoff between canary attempts, in seconds. 15m, 30m, 1h,
    #: 2h and then every 6h by default; the LAST value is the permanent cap.
    leaseweb_cloud_capacity_recovery_backoff_seconds: list[int] = Field(
        default_factory=lambda: [900, 1800, 3600, 7200, 21600]
    )
    #: How long ONE in-flight canary order holds the durable single-attempt
    #: lease before it expires on its own (a crashed worker never deadlocks
    #: recovery).
    leaseweb_cloud_capacity_canary_lease_seconds: int = Field(default=900, ge=60)
    #: Operator reminder cadence while an account stays blocked: first card
    #: after the delay, then every interval (deduplicated by the outbox).
    leaseweb_cloud_capacity_outage_reminder_delay_seconds: int = Field(default=1800, ge=60)
    leaseweb_cloud_capacity_outage_reminder_interval_seconds: int = Field(default=21600, ge=60)
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
    # Commercial product families per provider
    # (``[providers.<key>.families.<family>]`` sections): each family names
    # its billing model and customer-facing product name, e.g. leaseweb
    # ``vps`` (monthly) and ``cloud`` (hourly). Absent = one implicit family
    # per billing model found in the provider's sellable offers.
    provider_families: dict[str, dict[str, dict[str, str]]] = Field(default_factory=dict)
    # --- Automatic catalog sync + pricing policy (server-owned) --------------
    # The periodic coordinator refreshes provider costs, reprices auto-priced
    # offers with the configured markup and publishes eligible ones — no
    # manual sync-offers / price-book / enable commands needed afterwards.
    storefront_catalog_sync_enabled: bool = True
    storefront_catalog_sync_interval_seconds: int = Field(default=900, ge=60)
    #: Dedicated job timeout for ONE ``catalog_auto_sync`` cron run. The
    #: periodic refresh walks every product of every account/region, so it
    #: legitimately outlives the generic worker job timeout; the catalog is
    #: the ONLY job granted the longer budget (see
    #: :func:`cloud_platform.worker.settings.catalog_auto_sync_timeout`).
    storefront_catalog_sync_timeout_seconds: int = Field(default=600, gt=0)
    #: Safety margin (seconds) added to the catalog FX publication horizon. The
    #: horizon is derived (interval + clamped timeout + this margin), never
    #: hardcoded: a price published now must stay provable until the next
    #: scheduled refresh has finished, otherwise the storefront empties between
    #: two healthy syncs the moment its reference rate expires.
    storefront_catalog_fx_safety_margin_seconds: int = Field(default=300, ge=0)
    # ``[storefront.pricing.<provider>]`` sections as parsed above, e.g.
    # ``{"leaseweb": {"mode": "markup", "markup_percent": 25,
    # "auto_publish": True}}``. Absent = no automatic pricing/publication
    # for that provider (sync still refreshes its costs).
    storefront_pricing: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @property
    def catalog_fx_min_remaining_lifetime_seconds(self) -> int:
        """Minimum remaining reference-rate lifetime a NEW catalog price carries.

        Derived from the catalog cadence, never hardcoded: the next scheduled
        refresh starts at most ``interval`` seconds from a run and may take up
        to its clamped ``timeout`` to finish, plus a configured margin for
        scheduler jitter. A price written with less lifetime than this expires
        before the refresh that would have replaced it, which is exactly how the
        2026-09-26 Leaseweb storefront blackout happened with every provider
        read healthy.
        """
        interval = int(self.storefront_catalog_sync_interval_seconds)
        # A catalog run must not outlive its own cadence (the same clamp the
        # worker applies to the job timeout) and the margin is never negative.
        timeout = min(int(self.storefront_catalog_sync_timeout_seconds), interval)
        margin = max(int(self.storefront_catalog_fx_safety_margin_seconds), 0)
        return interval + timeout + margin

    arvancloud_api_key: str = ""
    arvancloud_api_base_url: str = "https://napi.arvancloud.ir/ecc/v1"
    arvancloud_region: str = ""
    provider_credential_encryption_key: str = ""
    zarinpal_enabled: bool = False
    zarinpal_merchant_id: str = ""
    zarinpal_base_url: str = "https://api.zarinpal.com/pg/v4/payment"
    zarinpal_sandbox: bool = False
    zarinpal_callback_url: str = ""
    tetraminator_enabled: bool = False
    tetraminator_api_key: str = ""
    tetraminator_base_url: str = "https://api.tetraminator.com/v1"
    tetraminator_callback_url: str = ""
    tetraminator_timeout_seconds: float = Field(default=30.0, gt=0)
    payment_gateway_secrets: dict[str, str] = Field(default_factory=dict)
    # --- Currency / FX resolution (`[fx]` + `[fx.abantether]`) ---------------
    # Platform-level financial subsystem (provider-neutral). The first live
    # source is AbanTether's public ticker (read-only, no secret). Amounts
    # stay integer minor units everywhere; the resolver owns every rate
    # formula and the only Toman/Euro/cent conversions.
    fx_enabled: bool = True
    #: Explicit family switches; ``fx_enabled`` remains a backwards-compatible
    #: master default for old configuration files.
    fx_domestic_enabled: bool = True
    fx_global_enabled: bool = True
    #: Legacy alias retained for existing TOML/environment deployments.
    fx_provider: str = "abantether"
    #: Preferred explicit name for the domestic/payment FX path.
    fx_domestic_provider: str | None = None
    fx_default_display_currency: str = "IRT"
    #: Server-owned canonical selling currency for foreign catalog offers.
    fx_catalog_pricing_currency: str = "USD"
    fx_quote_ttl_seconds: int = Field(default=60, gt=0)
    fx_max_stale_seconds: int = Field(default=300, gt=0)
    fx_charge_max_stale_seconds: int = Field(default=30, ge=0)
    fx_request_timeout_seconds: float = Field(default=5.0, gt=0)
    # USD has no verified fiat market: display may use the configured USDT
    # proxy explicitly (metadata proxy=true), settlement via the proxy stays
    # off unless the operator opts in.
    fx_allow_usdt_proxy_for_display: bool = True
    fx_allow_usdt_proxy_for_settlement: bool = False
    fx_abantether_base_url: str = "https://api.abantether.com"
    fx_abantether_eur_symbol: str = "EUR"
    fx_abantether_usd_proxy_symbol: str = "USDT"
    # Global fiat FX via Frankfurter (reference rates, no API key).
    fx_frankfurter_base_url: str = "https://api.frankfurter.dev"
    fx_frankfurter_request_timeout_seconds: int = Field(default=5, gt=0)
    fx_frankfurter_quote_ttl_seconds: int = Field(default=3600, gt=0)
    fx_frankfurter_max_stale_seconds: int = Field(default=345600, ge=0)
    fx_frankfurter_catalog_max_stale_seconds: int = Field(default=86400, ge=0)
    fx_global_fiat_provider: str = "frankfurter"
    default_currency: str = "USD"
    # The OPERATOR-declared price book the selling price is derived from
    # (M08-005): confirmation, holds and immutable server price snapshots
    # all read the SAME book. There is no versioned book by default.
    price_book_name: str = "retail-usd"
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
    # development environment only (the factory refuses it elsewhere, and a
    # production process resolving to it fails closed at Settings level).
    # Ownership: the VALUE stays server-owned (bot token, keys and URLs are
    # secrets and live only in configuration.toml), but the production
    # topology invariant (backend = redis) is enforced release-side: the
    # release-owned production compose sets TELEGRAM_SESSIONS_BACKEND=redis,
    # which wins over a stale historical TOML value by documented precedence.
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

    @model_validator(mode="after")
    def _normalize_leaseweb_accounts(self) -> Settings:
        """Validate and normalize the Leaseweb credential accounts.

        Rules (LEASEWEB-MULTIACCOUNT §config parser):

        - account ids are unique, stable and safe to log;
        - an enabled account must carry a non-empty API key;
        - the lifecycle state is one of active/draining/disabled;
        - at least one account must be enabled when accounts are configured;
        - the deprecated single ``api_key`` becomes account ``default`` when
          no explicit accounts exist, so legacy deployments and the rows they
          already created stay routable.
        """
        accounts: list[LeasewebAccountSettings] = []
        seen: set[str] = set()
        for account in self.leaseweb_accounts:
            account_id = (account.id or "").strip()
            if not _ACCOUNT_ID_PATTERN.match(account_id):
                raise ValueError(
                    f"leaseweb account id {account.id!r} is invalid: use 1-63 "
                    "characters of [A-Za-z0-9._-] starting alphanumeric"
                )
            if account_id in seen:
                raise ValueError(f"duplicate leaseweb account id: {account_id!r}")
            seen.add(account_id)
            state = account.normalized_state
            if state not in _ACCOUNT_STATES:
                raise ValueError(
                    f"leaseweb account {account_id!r} has invalid state {state!r}: "
                    "expected one of active, draining, disabled"
                )
            if account.enabled and not account.has_credential:
                raise ValueError(f"leaseweb account {account_id!r} is enabled but has no api_key")
            account.id = account_id
            account.state = state
            account.label = (account.label or "").strip()
            accounts.append(account)
        if accounts and not any(account.enabled for account in accounts):
            raise ValueError(
                "leaseweb accounts are configured but none is enabled; at least one "
                "enabled account is required to serve the provider"
            )
        if not accounts and self.leaseweb_api_key.strip():
            accounts.append(
                LeasewebAccountSettings(
                    id=DEFAULT_CREDENTIAL_ACCOUNT_ID,
                    api_key=self.leaseweb_api_key,
                    enabled=True,
                    priority=100,
                    state="active",
                )
            )
        self.leaseweb_accounts = accounts
        return self

    @property
    def leaseweb_new_order_accounts(self) -> list[LeasewebAccountSettings]:
        """Enabled accounts that may receive NEW orders (priority order)."""
        return sorted(
            (account for account in self.leaseweb_accounts if account.accepts_new_orders),
            key=lambda account: (account.priority, account.id),
        )

    @property
    def leaseweb_managed_accounts(self) -> list[LeasewebAccountSettings]:
        """Accounts that may still manage the resources they already own."""
        return [account for account in self.leaseweb_accounts if account.usable]

    @property
    def leaseweb_configured(self) -> bool:
        """Whether ANY usable Leaseweb credential account is configured."""
        return any(account.enabled and account.has_credential for account in self.leaseweb_accounts)

    @model_validator(mode="after")
    def _enforce_production_session_backend(self) -> Settings:
        """Production must use the shared Redis transient-state backend.

        Backend names are accepted case-insensitively (the factory
        convention) and normalized to the canonical value. After all
        precedence has been applied, a production process that still
        resolves to ``memory`` fails closed: the release-owned production
        compose injects ``TELEGRAM_SESSIONS_BACKEND=redis`` (environment
        beats a stale TOML), so reaching this error means the deployment
        topology itself is wrong. Development/test may keep ``memory``.
        """
        backend = (self.telegram_sessions_backend or "").strip().lower()
        if backend in ("redis", "memory"):
            self.telegram_sessions_backend = backend
        if (self.app_env or "").strip().lower() == "production" and backend == "memory":
            raise ValueError(
                "telegram_sessions_backend must be 'redis' when app_env is "
                "'production' (production compose sets "
                "TELEGRAM_SESSIONS_BACKEND=redis)"
            )
        return self

    @model_validator(mode="after")
    def _validate_production_payment_configuration(self) -> Settings:
        """An ENABLED payment gateway must be usable in the resolved environment.

        A gateway is either off (any placeholder may stay in the file) or fully
        configured. Catching it here — at the configuration boundary — means a
        bad production callback URL fails the deployment preflight instead of
        surfacing the first time a customer presses "pay".

        Tetraminator calls its callback itself, with no signature and no
        credentials, so in production that URL must be an absolute public
        ``https://`` one: plain http could be read or forged in transit and
        would have to be rejected at request time anyway. Development and test
        may use http. Secrets are never echoed in the error text.
        """
        if not self.tetraminator_enabled:
            return self._validate_zarinpal_configuration()
        from urllib.parse import urlsplit

        if not (self.tetraminator_api_key or "").strip():
            raise ValueError(
                "payments.tetraminator.api_key is required when "
                "payments.tetraminator.enabled is true"
            )
        if self.tetraminator_timeout_seconds <= 0:
            raise ValueError("payments.tetraminator.timeout_seconds must be greater than 0")

        base_url = urlsplit((self.tetraminator_base_url or "").strip())
        if base_url.scheme not in ("http", "https") or not base_url.netloc:
            raise ValueError("payments.tetraminator.base_url must be an absolute http(s) URL")

        callback_url = urlsplit((self.tetraminator_callback_url or "").strip())
        if callback_url.scheme not in ("http", "https") or not callback_url.netloc:
            raise ValueError(
                "payments.tetraminator.callback_url must be an absolute http(s) URL: "
                "it is the address Tetraminator calls after a payment"
            )
        if (self.app_env or "").strip().lower() == "production" and callback_url.scheme != "https":
            raise ValueError(
                "payments.tetraminator.callback_url must use https:// when app_env is "
                "'production' (the callback is unauthenticated and must be publicly "
                "reachable; use the API's reverse-proxied domain)"
            )
        return self._validate_zarinpal_configuration()

    def _validate_zarinpal_configuration(self) -> Settings:
        """Validate the enabled ZarinPal gateway without exposing secrets."""
        if not self.zarinpal_enabled:
            return self
        from urllib.parse import urlsplit

        if (
            not (self.zarinpal_merchant_id or "").strip()
            or (self.zarinpal_merchant_id or "").strip() == "CHANGE_ME"
        ):
            raise ValueError(
                "payments.zarinpal.merchant_id is required when payments.zarinpal.enabled is true"
            )
        base_url = urlsplit((self.zarinpal_base_url or "").strip())
        if base_url.scheme not in ("http", "https") or not base_url.netloc:
            raise ValueError("payments.zarinpal.base_url must be an absolute http(s) URL")
        callback_url = urlsplit((self.zarinpal_callback_url or "").strip())
        if callback_url.scheme not in ("http", "https") or not callback_url.netloc:
            raise ValueError(
                "payments.zarinpal.callback_url must be an absolute http(s) URL: "
                "it is the address ZarinPal calls after a payment"
            )
        if (self.app_env or "").strip().lower() == "production" and callback_url.scheme != "https":
            raise ValueError("payments.zarinpal.callback_url must use https:// in production")
        return self

    @model_validator(mode="after")
    def _validate_fx_configuration(self) -> Settings:
        """Validate the platform FX section (read-only source, no secret)."""
        from urllib.parse import urlsplit

        legacy_provider = (self.fx_provider or "").strip().lower()
        domestic_alias = (self.fx_domestic_provider or "").strip().lower()
        if domestic_alias and legacy_provider != "abantether" and domestic_alias != legacy_provider:
            raise ValueError(
                "fx.domestic_provider conflicts with legacy fx.provider; configure one source"
            )
        provider = domestic_alias or legacy_provider
        if provider != "abantether":
            raise ValueError(
                "fx.domestic_provider must be 'abantether'; global fiat uses "
                "fx.global_fiat_provider and remains a separate route"
            )
        self.fx_provider = provider
        self.fx_domestic_provider = provider
        if self.fx_quote_ttl_seconds <= 0:
            raise ValueError("fx.quote_ttl_seconds must be greater than 0")
        if self.fx_max_stale_seconds < self.fx_quote_ttl_seconds:
            raise ValueError("fx.max_stale_seconds must be >= fx.quote_ttl_seconds")
        if self.fx_enabled and self.fx_charge_max_stale_seconds <= 0:
            raise ValueError("fx.charge_max_stale_seconds must be greater than 0")
        if self.fx_charge_max_stale_seconds > self.fx_max_stale_seconds:
            raise ValueError("fx.charge_max_stale_seconds must be <= fx.max_stale_seconds")
        if (
            not math.isfinite(float(self.fx_request_timeout_seconds))
            or self.fx_request_timeout_seconds <= 0
        ):
            raise ValueError("fx.request_timeout_seconds must be a positive finite number")
        self.fx_request_timeout_seconds = float(math.ceil(self.fx_request_timeout_seconds))

        global_provider = (self.fx_global_fiat_provider or "").strip().lower()
        if global_provider not in ("frankfurter",):
            raise ValueError(
                "fx.global_fiat_provider must be a known global fiat source "
                "('frankfurter'); adding a source is a code change, not a config edit"
            )
        self.fx_global_fiat_provider = global_provider

        base = urlsplit((self.fx_abantether_base_url or "").strip())
        if base.scheme not in ("http", "https") or not base.netloc:
            raise ValueError("fx.abantether.base_url must be an absolute http(s) URL")
        if (
            base.username
            or base.password
            or base.query
            or base.fragment
            or any(character.isspace() for character in (self.fx_abantether_base_url or ""))
        ):
            raise ValueError("fx.abantether.base_url must not contain credentials/query/fragment")
        try:
            port = base.port
        except ValueError as exc:
            raise ValueError("fx.abantether.base_url has an invalid port") from exc
        if port is not None and not 1 <= port <= 65_535:
            raise ValueError("fx.abantether.base_url has an invalid port")
        if (self.app_env or "").strip().lower() == "production" and base.scheme != "https":
            raise ValueError(
                "fx.abantether.base_url must use https:// when app_env is 'production'"
            )
        if not (self.fx_abantether_eur_symbol or "").strip():
            raise ValueError("fx.abantether.eur_symbol must not be empty")
        proxy_enabled = (
            self.fx_allow_usdt_proxy_for_display or self.fx_allow_usdt_proxy_for_settlement
        )
        if proxy_enabled and not (self.fx_abantether_usd_proxy_symbol or "").strip():
            raise ValueError(
                "fx.abantether.usd_proxy_symbol must not be empty when the USD proxy is enabled"
            )
        frankfurter_base = urlsplit((self.fx_frankfurter_base_url or "").strip())
        if frankfurter_base.scheme not in ("http", "https") or not frankfurter_base.netloc:
            raise ValueError("fx.frankfurter.base_url must be an absolute http(s) URL")
        if any(character.isspace() for character in (self.fx_frankfurter_base_url or "")):
            raise ValueError("fx.frankfurter.base_url must not contain whitespace")
        try:
            frankfurter_port = frankfurter_base.port
        except ValueError as exc:
            raise ValueError("fx.frankfurter.base_url has an invalid port") from exc
        if frankfurter_port is not None and not 1 <= frankfurter_port <= 65_535:
            raise ValueError("fx.frankfurter.base_url has an invalid port")
        prod_env = (self.app_env or "").strip().lower() == "production"
        if prod_env and frankfurter_base.scheme != "https":
            raise ValueError(
                "fx.frankfurter.base_url must use https:// when app_env is 'production'"
            )
        if self.fx_frankfurter_quote_ttl_seconds <= 0:
            raise ValueError("fx.frankfurter.quote_ttl_seconds must be > 0")
        try:
            timeout_decimal = Decimal(str(self.fx_frankfurter_request_timeout_seconds))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError(
                "fx.frankfurter.request_timeout_seconds must be a positive finite number"
            ) from exc
        if not timeout_decimal.is_finite() or timeout_decimal <= 0:
            raise ValueError(
                "fx.frankfurter.request_timeout_seconds must be a positive finite number"
            )
        if self.fx_frankfurter_max_stale_seconds < self.fx_frankfurter_quote_ttl_seconds:
            raise ValueError("fx.frankfurter.max_stale_seconds must be >= quote_ttl_seconds")
        if self.fx_frankfurter_catalog_max_stale_seconds < self.fx_frankfurter_quote_ttl_seconds:
            raise ValueError(
                "fx.frankfurter.catalog_max_stale_seconds must be >= quote_ttl_seconds"
            )
        if self.fx_frankfurter_catalog_max_stale_seconds > self.fx_frankfurter_max_stale_seconds:
            raise ValueError(
                "fx.frankfurter.catalog_max_stale_seconds must be <= max_stale_seconds"
            )
        if frankfurter_base.username or frankfurter_base.password:
            raise ValueError("fx.frankfurter.base_url must not contain userinfo")
        if frankfurter_base.query or frankfurter_base.fragment:
            raise ValueError("fx.frankfurter.base_url must not contain a query or fragment")
        display = (self.fx_default_display_currency or "").strip().upper()
        if display not in ("IRT", "IRR", "EUR", "GBP", "JPY", "SGD", "AUD", "CAD", "USD", "KRW"):
            raise ValueError("fx.default_display_currency is not a supported display currency")
        self.fx_default_display_currency = display
        target = (self.fx_catalog_pricing_currency or "").strip().upper()
        if target not in ("EUR", "GBP", "JPY", "SGD", "AUD", "CAD", "USD", "KRW"):
            raise ValueError("fx.catalog_pricing_currency must be an audited global fiat code")
        self.fx_catalog_pricing_currency = target
        return self

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


def looks_corrupted_label(value: object) -> bool:
    """Whether a configured customer-facing label is obviously corrupted.

    Operator configuration carries the names customers read (provider
    ``display_name``, product-family ``display_name``). A value made only of
    question marks, replacement characters, punctuation or whitespace —
    ``"????"``, ``"????????"``, ``"..."`` — is the signature of a broken
    terminal paste, not a name, and must never be rendered.

    The rule is deliberately locale-neutral: the label only has to contain at
    least one letter or digit, so ``Leaseweb``, ``General Purpose``,
    ``وی‌پی‌اس`` and ``کلود`` all pass, while ``????????`` fails. An empty
    value is NOT corrupted: it means "not configured" and the caller applies
    its own fallback.
    """
    text = str(value if value is not None else "").strip()
    if not text:
        return False
    return not any(character.isalnum() for character in text)


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

"""Operator CLI for the LEASEWEB-MVP (and the pieces around it).

Run with::

    uv run python -m cloud_platform.cli <command> ...

Commands::

    leaseweb doctor              Pre-flight diagnostics (read-only; never
    leaseweb accounts list       Safe credential-account inventory (no keys)
    leaseweb accounts doctor     Per-account authentication pre-flight
    fx doctor                    Currency/FX pre-flight (AbanTether + Frankfurter, cache)
    fx rates [--target USD]       Read-only global reference rates (target must match config)
    leaseweb auth-check          Read-only API key check
    leaseweb coverage            Print the VPS API endpoint coverage matrix
    leaseweb products list       Read-only ordering catalogue
    leaseweb products show ID    Read-only product configuration + prices
    leaseweb orders list|show    Read-only account order inspection
    leaseweb vps list|show       Read-only VPS inventory
    leaseweb vps ips|metrics     Read-only VPS IPs / data-traffic metrics
    leaseweb vps snapshots       Read-only VPS snapshot list
    leaseweb vps monitoring      Read-only VPS monitoring status
                                 prints the API key, never orders anything)
    leaseweb sync-offers         Refresh the sellable-offer price book from
                                 the Leaseweb ordering API
    offers list [--all]          Sellable offers (or all rows)
    offers doctor                Read-only: why the customer catalog is empty
    offers preview [--market M]  Print the catalog as the BOT would render it
    offers price-book --markup-percent N    Bulk-price UNPRICED offers from cost
    offers enable <offer_id>     Operator-enable an offer
    offers disable <offer_id>    Hide an offer from sale
    offers price <offer_id> <minor> [currency]   Set the customer price
    users find <telegram_id>     Resolve a user by Telegram id
    wallet balance <user_id>     Show a user's wallet
    wallet credit <user_id> <minor> <reason>     Ledger-safe admin credit
    wallet debit  <user_id> <minor> <reason>     Ledger-safe admin debit
    orders list                  Open provider orders
    orders attention             FAILED + NEEDS_REVIEW orders
    orders inspect <order_id>    One order with its server/operation
    orders retry <order_id>      Reopen a DEFINITIVELY FAILED order intent
                                 (same local operation key; no provider dedup)
    orders resolve-existing <order_id> <provider_order_id> --reason R --yes
                                 Attach a provider order id a human verified
                                 (ambiguous POST; read-only validation, no POST)
    orders resolve-vps <order_id> <vps_id> --reason R --yes
                                 Attach the provisioned VPS id a human verified
                                 at the portal (read-only VPS validation, no
                                 POST; requires settlement complete)
    orders resolve-not-created <order_id> --reason R --yes
                                 Re-queue an ambiguous intent after the human
                                 verified the provider created nothing
    renewals list [--attention]  Renewal records (attention queue)
    renewals check               Run the daily renewal pass right now

Financial mutations go through the existing application services
(``WalletAdminService``, the operation ledger) — never ad-hoc SQL.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from cloud_platform.core.config import get_settings

logger = logging.getLogger("cli")


def _csv(value: str) -> tuple[str, ...]:
    """Parse a comma-separated setting into a tuple of trimmed parts."""
    return tuple(part.strip() for part in (value or "").split(",") if part.strip())


def _valid_currency(value: object) -> str | None:
    """3-letter uppercase ISO code or None (never inferred, never converted)."""
    code = str(value or "").strip().upper()
    if len(code) != 3 or not code.isalpha():
        return None
    return code


def _redact(value: str) -> str:
    """Show only the last 4 chars of a secret (never the full value)."""
    if not value:
        return "<not set>"
    if len(value) <= 4:
        return "*" * len(value)
    return "*" * (len(value) - 4) + value[-4:]


# ---------------------------------------------------------------------------
# leaseweb doctor — read-only pre-flight
# ---------------------------------------------------------------------------


async def _check_db() -> tuple[bool, str]:
    try:
        from sqlalchemy import text

        from cloud_platform.db.session import SessionFactory

        async with SessionFactory() as session:
            await session.execute(text("SELECT 1"))
        return True, "ok"
    except Exception as exc:
        return False, str(exc)


async def _check_redis() -> tuple[bool, str]:
    try:
        from cloud_platform.core.redis import close_redis_client, create_redis_client

        client = create_redis_client(get_settings().redis_url)
        try:
            await client.ping()
        finally:
            await close_redis_client(client)
        return True, "ok"
    except Exception as exc:
        return False, str(exc)


async def _check_session_store() -> tuple[bool, str]:
    """Round-trip the Telegram transient-state backend (never a secret)."""
    from cloud_platform.core.session_store import (
        NS_REFERENCE,
        build_session_store,
    )

    settings = get_settings()
    try:
        store = build_session_store(
            backend=settings.telegram_sessions_backend,
            prefix=settings.telegram_sessions_namespace,
            redis_url=settings.redis_url,
        )
        await store.put(NS_REFERENCE, "doctor-probe", {"ref": "probe"}, ttl_seconds=30)
        value = await store.get(NS_REFERENCE, "doctor-probe")
        await store.delete(NS_REFERENCE, "doctor-probe")
        client = getattr(store, "client", None)
        if client is not None:
            from cloud_platform.core.redis import close_redis_client

            await close_redis_client(client)
        if value is None:
            return False, "write succeeded but the read returned nothing"
        return True, f"{settings.telegram_sessions_backend} read/write ok"
    except Exception as exc:
        # The backend refuses `memory` outside development; that refusal is
        # itself the diagnostic, and it must stay readable.
        return False, str(exc)


@dataclass(frozen=True, slots=True)
class DoctorResult:
    ok: bool
    lines: list[str]


async def leaseweb_doctor() -> DoctorResult:
    """Read-only pre-flight: key presence, auth, locations, products, DB, Redis."""
    settings = get_settings()
    lines: list[str] = []
    ok = True

    def report(name: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and passed
        mark = "OK " if passed else "FAIL"
        lines.append(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))

    def note(mark: str, name: str, detail: str = "") -> None:
        lines.append(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))

    # ``leaseweb_accounts`` is always populated when a credential exists (the
    # deprecated single api_key normalizes into account ``default``), so an
    # empty list means "nothing configured". Settings objects that predate
    # credential accounts keep the original single-key wording.
    accounts = list(getattr(settings, "leaseweb_accounts", None) or ())
    legacy_single = not accounts
    if legacy_single and not settings.leaseweb_api_key:
        report("LEASEWEB_API_KEY configured", False, "set it in .env or the environment")
        lines.append("\nAction: add LEASEWEB_API_KEY=<key> to .env (from your Leaseweb portal).")
        return DoctorResult(ok, lines)
    if legacy_single:
        report(
            "LEASEWEB_API_KEY configured",
            True,
            f"set ({_redact(settings.leaseweb_api_key)})",
        )
    elif len(accounts) == 1 and accounts[0].id == "default":
        report(
            "LEASEWEB_API_KEY configured",
            True,
            f"set ({_redact(accounts[0].api_key)})",
        )
    else:
        report(
            "Leaseweb credentials configured",
            True,
            f"{len(accounts)} account(s): " + ", ".join(account.id for account in accounts),
        )

    from cloud_platform.providers.leaseweb.errors import LeasewebAuthenticationError
    from cloud_platform.providers.leaseweb.ordering import (
        KNOWN_VPS_DATACENTERS,
        LocationEligibility,
        merge_candidates,
    )
    from cloud_platform.providers.leaseweb.ordering_sync import ordering_provider_from_settings

    # LEASEWEB-MULTIACCOUNT: authentication is a PER-ACCOUNT fact. One rejected
    # key must read as DEGRADED, not as a dead provider, and must not hide the
    # locations served by the keys that still work.
    router = None if legacy_single else _leaseweb_account_router_factory()(settings)
    if router is not None and len(router.accounts) > 1:
        health = await router.verify_all()
        for entry in health.accounts:
            if entry.ok:
                note("OK ", f"account {entry.account_id} authentication")
            else:
                note("FAIL", f"account {entry.account_id} authentication", entry.error_class or "")
        if health.status == "ok":
            note("OK ", "Leaseweb aggregate", f"{len(health.healthy)} account(s) usable")
        elif health.healthy:
            note(
                "WARN",
                "Leaseweb overall",
                f"DEGRADED — {len(health.healthy)}/{len(health.accounts)} account(s) usable",
            )
        else:
            report(
                "Leaseweb overall",
                False,
                "UNAVAILABLE — no credential account authenticated",
            )
            return DoctorResult(ok, lines)

    provider = (
        next(iter(router.ordered_providers.values()))
        if router is not None and router.ordered_providers
        else ordering_provider_from_settings(settings)
    )
    # Candidate discovery mirrors the catalog sync: configured seeds are
    # hints only; eligibility is decided per location by live probes.
    candidates = merge_candidates(tuple(provider.discovery_seeds), KNOWN_VPS_DATACENTERS)
    unscoped_ok: bool | None = None
    try:
        unscoped = await provider.list_products_unscoped()
        unscoped_ok = True
        for product in unscoped:
            if product.location and product.location not in candidates:
                candidates += (product.location,)
    except LeasewebAuthenticationError:
        unscoped_ok = False
    except Exception as exc:
        note("INFO", "Unscoped catalog", f"unavailable ({type(exc).__name__})")

    probes: dict[str, Any] = {}
    queue = list(candidates)
    while queue:
        location = queue.pop(0)
        if location in probes:
            continue
        try:
            probe = await provider.probe_location(location)
        except Exception as exc:
            note("WARN", f"Products at {location}", f"probe failed ({type(exc).__name__})")
            continue
        probes[location] = probe
        if probe.eligibility is LocationEligibility.FATAL_AUTHENTICATION:
            break
        for extra in probe.discovered_locations:
            if extra not in probes and extra not in queue:
                queue.append(extra)
                candidates += (extra,)

    definitive = [
        probe
        for probe in probes.values()
        if probe.eligibility
        in (
            LocationEligibility.ELIGIBLE_AVAILABLE,
            LocationEligibility.ELIGIBLE_EMPTY,
            LocationEligibility.INELIGIBLE_ACCOUNT,
        )
    ]
    fatal = [
        probe
        for probe in probes.values()
        if probe.eligibility is LocationEligibility.FATAL_AUTHENTICATION
    ]
    if definitive:
        report(
            "Ordering API reachable",
            True,
            f"{len(definitive)} location(s) answered decisively",
        )
    elif fatal or unscoped_ok is False:
        report("Ordering API reachable", False, "authentication rejected — check the API key")
        report(
            "Authentication works",
            False,
            "X-LSW-Auth rejected — check the API key, or that your account is "
            "eligible for the VPS Ordering API (post-payment enabled). "
            "See docs/leaseweb/INTEGRATION_NOTES.md.",
        )
        return DoctorResult(ok, lines)
    else:
        report("Ordering API reachable", False, "no location answered decisively")
        return DoctorResult(ok, lines)
    report("Authentication works", True, "products endpoint accepted X-LSW-Auth")

    usable = 0
    for location in candidates:
        seen = probes.get(location)
        if seen is None:
            continue
        if seen.eligibility is LocationEligibility.ELIGIBLE_AVAILABLE:
            usable += 1
            note("INFO", location, f"available, {len(seen.products)} product(s)")
        elif seen.eligibility is LocationEligibility.ELIGIBLE_EMPTY:
            note("INFO", location, "eligible, no products right now")
        elif seen.eligibility is LocationEligibility.INELIGIBLE_ACCOUNT:
            note("SKIP", location, "not enabled for this sales organization")
        else:
            note("WARN", location, f"catalog check inconclusive ({seen.note})")
    if usable:
        report("Eligible locations", True, f"{usable} usable location(s)")
    else:
        report(
            "Eligible locations",
            False,
            "no usable ordering locations — the account may "
            "not be eligible for the VPS Ordering API yet",
        )
        lines.append(
            "\nAction: verify eligibility in the Leaseweb Customer Portal "
            "(ordering/VPS must be post-payment enabled)."
        )

    from cloud_platform.db.session import SessionFactory
    from cloud_platform.modules.offers.repository import SqlAlchemySellableOfferRepository

    offers_repo = SqlAlchemySellableOfferRepository(SessionFactory)
    for location in candidates:
        cached = probes.get(location)
        if cached is None or cached.eligibility is not LocationEligibility.ELIGIBLE_AVAILABLE:
            continue
        if not cached.products:
            continue
        first = cached.products[0]
        try:
            detail = await provider.get_product(location, first.id)
        except Exception as exc:
            report(
                f"Product detail {first.id}@{location}",
                False,
                f"price/options retrieval failed: {exc}",
            )
            continue
        free_os = len(detail.free_os_options())
        report(
            f"Product detail {first.id}@{location}",
            True,
            f"monthly {detail.product.monthly_price_minor} {detail.product.currency}; "
            f"{len(detail.os_options)} OS option(s), {free_os} free",
        )

    try:
        offers = await offers_repo.list_all()
    except Exception as exc:
        report("Database reachable", False, str(exc))
    else:
        report("Database reachable", True, f"{len(offers)} sellable-offer row(s)")
        synced = [o for o in offers if o.provider_available]
        enabled = sum(1 for o in synced if o.enabled)
        report(
            "Synced offers (price book)",
            True,
            f"{len(synced)} provider-available row(s); {enabled} enabled",
        )

    db_ok, db_detail = await _check_db()
    report("Database query", db_ok, db_detail)
    redis_ok, redis_detail = await _check_redis()
    report("Redis reachable", redis_ok, redis_detail)
    # PROD-HARDENING §10: the bot's shared transient state must be the durable
    # backend in production, because a restart or a second replica otherwise
    # loses the buttons a customer is holding (and confirmations with them).
    sessions_ok, sessions_detail = await _check_session_store()
    report("Telegram sessions", sessions_ok, sessions_detail)
    if settings.telegram_sessions_backend.strip().lower() != "redis":
        report(
            "Telegram sessions shared",
            False,
            f"backend={settings.telegram_sessions_backend!r} — a production "
            "deployment needs `redis` ([telegram.sessions] backend)",
        )

    if not ok:
        lines.append(
            "\nFix the FAIL items above, then re-run: "
            "uv run python -m cloud_platform.cli leaseweb doctor"
        )
    return DoctorResult(ok, lines)


async def fx_rates(target: str | None = None) -> int:
    """Read one exact global reference rate for each supported base currency."""
    from cloud_platform.core.container import Container, create_container
    from cloud_platform.modules.fx.domain import GLOBAL_FIAT_CURRENCIES, FxError

    settings = get_settings()
    if not (settings.fx_enabled and settings.fx_global_enabled):
        print("Global FX is disabled ([fx] global_enabled = false or enabled = false)")
        return 1
    configured_target = (settings.fx_catalog_pricing_currency or "USD").strip().upper()
    target = (target or configured_target).strip().upper()
    if target != configured_target:
        print(
            f"refused: --target {target} differs from configured catalog currency "
            f"{configured_target}"
        )
        return 2
    if target not in GLOBAL_FIAT_CURRENCIES:
        print(f"unsupported target currency: {target}")
        return 1
    container = create_container()
    resolver = container.global_fx_resolver_or_none()
    if resolver is None:
        await container.close()
        print("global FX resolver unavailable")
        return 1
    failures = 0
    try:
        for base in sorted(GLOBAL_FIAT_CURRENCIES - {target}):
            try:
                resolution = await resolver.get_rate(base, target)
                print(
                    f"{base}/{target}: rate={resolution.rate} "
                    f"provider_date={resolution.quote.provider_date.isoformat()} "
                    f"source={resolution.source} stale={resolution.stale}"
                )
            except Exception as exc:
                failures += 1
                print(f"{base}/{target}: error ({type(exc).__name__})")
        return 1 if failures else 0
    except FxError as exc:
        print(f"FX rates unavailable ({type(exc).__name__})")
        return 1
    finally:
        await Container.aclose_fx(resolver)
        await container.close()


async def fx_doctor() -> DoctorResult:
    """Read-only FX pre-flight: config, AbanTether markets, proxy, cache.

    Never performs a trade, order or payment; only reads the public ticker
    and the cache backend. Never prints a live price payload in full.
    """
    from cloud_platform.modules.fx.cache import build_fx_cache

    settings = get_settings()
    lines: list[str] = []
    ok = True

    def report(name: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and passed
        mark = "OK " if passed else "FAIL"
        lines.append(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))

    def note(mark: str, name: str, detail: str = "") -> None:
        lines.append(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))

    if not settings.fx_enabled:
        note("SKIP", "All FX families", "disabled ([fx] enabled = false)")
    if not (settings.fx_enabled and settings.fx_domestic_enabled):
        # The master switch wins, exactly as it does for the container's
        # FxConfig and for the global family below. A read-only pre-flight must
        # never call the public ticker for an operator-disabled family.
        note(
            "SKIP",
            "Domestic FX provider",
            "disabled ([fx] domestic_enabled/enabled = false)",
        )
    else:
        legacy_provider = (settings.fx_provider or "").strip().lower()
        domestic_provider = (settings.fx_domestic_provider or "").strip().lower()
        if domestic_provider and legacy_provider and domestic_provider != legacy_provider:
            report(
                "FX provider",
                False,
                "legacy fx.provider and fx.domestic_provider disagree",
            )
        else:
            provider = domestic_provider or legacy_provider
            if provider != "abantether":
                report("FX provider", False, f"unknown provider {provider!r}")
            else:
                report("FX provider", True, "AbanTether configured (read-only ticker, no key)")

            from cloud_platform.providers.abantether_fx.client import AbanTetherFxClient

            client = AbanTetherFxClient(
                base_url=settings.fx_abantether_base_url,
                timeout_seconds=float(settings.fx_request_timeout_seconds),
                ttl_seconds=settings.fx_quote_ttl_seconds,
                eur_symbol=settings.fx_abantether_eur_symbol,
                usd_proxy_symbol=settings.fx_abantether_usd_proxy_symbol,
            )
            try:
                try:
                    eur = await client.get_quote("EUR", "IRT")
                except Exception as exc:
                    report("EUR/IRT", False, f"{type(exc).__name__}")
                else:
                    report("EUR/IRT", True, f"active (market {eur.source_market})")
                if (
                    settings.fx_allow_usdt_proxy_for_display
                    or settings.fx_allow_usdt_proxy_for_settlement
                ):
                    try:
                        usdt = await client.get_quote(
                            settings.fx_abantether_usd_proxy_symbol, "IRT"
                        )
                    except Exception as exc:
                        report("USD display proxy", False, f"{type(exc).__name__}")
                    else:
                        scope = (
                            "display+settlement"
                            if settings.fx_allow_usdt_proxy_for_settlement
                            else "display only"
                        )
                        report(
                            "USD display proxy",
                            True,
                            f"USDT/IRT active ({scope}; proxy=true)",
                        )
                        _ = usdt
                else:
                    note("SKIP", "USD display proxy", "disabled by configuration")
            finally:
                await client.close()

    # Global reference-rate family is independent of the domestic/payment path.
    target = (settings.fx_catalog_pricing_currency or "USD").strip().upper()
    report(
        "Global FX target",
        target in {"EUR", "GBP", "JPY", "SGD", "AUD", "CAD", "USD", "KRW"},
        target,
    )
    global_provider = (settings.fx_global_fiat_provider or "").strip().lower()
    if not (settings.fx_enabled and settings.fx_global_enabled):
        note("SKIP", "Global FX provider", "disabled ([fx] global_enabled/enabled = false)")
    elif global_provider != "frankfurter":
        report("Global FX provider", False, f"unknown provider {global_provider!r}")
    else:
        report("Global FX provider", True, "Frankfurter reference rates configured")
        try:
            from cloud_platform.core.container import Container, create_container
            from cloud_platform.modules.fx.domain import GLOBAL_FIAT_CURRENCIES

            global_container = None
            global_resolver = None
            try:
                global_container = create_container()
                global_resolver = global_container.global_fx_resolver_or_none()
                if global_resolver is None:
                    report("Global FX probe", False, "resolver unavailable")
                else:
                    for base in sorted(GLOBAL_FIAT_CURRENCIES - {target}):
                        resolution = await global_resolver.get_rate(base, target)
                        if resolution.rate <= 0:
                            report(f"Global FX {base}/{target}", False, "non-positive rate")
                        else:
                            report(
                                f"Global FX {base}/{target}",
                                True,
                                f"rate={resolution.rate} stale={resolution.stale}",
                            )
            finally:
                await Container.aclose_fx(global_resolver)
                if global_container is not None:
                    await global_container.close()
        except Exception as exc:
            report("Global FX probe", False, type(exc).__name__)

    # Cache reachability (production shares Redis; dev/test use memory).
    try:
        backend = "redis" if (settings.app_env or "").strip().lower() == "production" else "memory"
        cache = build_fx_cache(backend=backend, redis_url=settings.redis_url)
        try:
            from cloud_platform.modules.fx.cache import InMemoryFxCache

            if isinstance(cache, InMemoryFxCache):
                report("FX cache", True, "memory (dev/test)")
            else:
                # Read-only probe: a miss still proves the backend answers.
                from cloud_platform.modules.fx.ports import fx_cache_key

                await cache.get(fx_cache_key("EUR", "IRT", source="abantether"))
                report("FX cache", True, f"{backend} reachable")
        finally:
            closer = getattr(cache, "close", None)
            if callable(closer):
                await closer()
    except Exception as exc:
        report("FX cache", False, f"{type(exc).__name__}")

    # Existing wallet currencies (report only; never mutates balances).
    try:
        from cloud_platform.db.session import SessionFactory
        from cloud_platform.modules.wallet.repository import SqlAlchemyWalletRepository

        wallets = await SqlAlchemyWalletRepository(SessionFactory).list_all()
        counts: dict[str, int] = {}
        for wallet in wallets:
            counts[wallet.currency] = counts.get(wallet.currency, 0) + 1
        if counts:
            summary = ", ".join(f"{c}: {n}" for c, n in sorted(counts.items()))
            note("INFO", "Wallet currencies", summary)
        else:
            note("INFO", "Wallet currencies", "no wallets yet")
    except Exception as exc:
        note("WARN", "Wallet currencies", f"unavailable ({type(exc).__name__})")

    if not ok:
        lines.append(
            "\nFix the FAIL items above, then re-run: uv run python -m cloud_platform.cli fx doctor"
        )
    return DoctorResult(ok, lines)


# ---------------------------------------------------------------------------
# leaseweb sync-offers
# ---------------------------------------------------------------------------


async def leaseweb_sync_offers() -> int:
    from cloud_platform.db.session import SessionFactory
    from cloud_platform.providers.leaseweb.accounts import build_leaseweb_account_router
    from cloud_platform.providers.leaseweb.ordering_sync import (
        LeaseWebOrderingCatalogSyncer,
        ordering_provider_from_settings,
    )

    settings = get_settings()
    router = build_leaseweb_account_router(settings)
    if router is not None:
        # LEASEWEB-MULTIACCOUNT: refresh EVERY configured credential account and
        # merge the observations into the one customer-facing Leaseweb catalog.
        print(f"leaseweb credential accounts: {', '.join(router.account_ids)}")
        syncer = LeaseWebOrderingCatalogSyncer(
            SessionFactory,
            accounts=router.ordered_providers,
            account_priorities=router.priorities,
            account_states=router.account_states,
        )
    elif settings.leaseweb_api_key:
        provider = ordering_provider_from_settings(settings)
        syncer = LeaseWebOrderingCatalogSyncer(SessionFactory, provider)
    else:
        print("No Leaseweb credential account is configured; cannot sync.")
        return 1
    result = await syncer.sync_all()
    # Per-credential, per-location evidence: an operator must be able to see
    # that EACH key really served the locations it was probed for, and that a
    # failure in one key did not disturb the others.
    persistence_failures: list[str] = []
    for name, step in result.items():
        print(
            f"{name}: fetched={step.total_fetched} upserted={step.total_upserted} "
            f"skipped={step.total_skipped}"
        )
        for account_id, locations in sorted(step.account_locations.items()):
            served = ", ".join(
                f"{location}: {count} products" for location, count in sorted(locations.items())
            )
            print(f"  account {account_id}: {served or 'no sellable locations'}")
        if name == "products":
            # DISCOVERED and PERSISTED are different facts: a run that read the
            # provider but failed to write must never look like a good sync.
            print(f"  discovered: {step.total_fetched} product observation(s)")
            print(f"  persisted : {step.offers_persisted} offer(s) written")
            print(f"  routes    : {step.routes_persisted} routing observation(s) written")
            if step.availability_reconciled:
                print(
                    f"  retired   : {step.marked_unavailable} offer(s) marked provider-unavailable"
                )
            else:
                print("  retired   : none (availability left untouched this run)")
        for warning in step.warnings:
            print(f"  warning: {warning}")
        for failure in step.persistence_failures:
            print(f"  PERSISTENCE FAILURE: {failure}")
            persistence_failures.append(f"{name}: {failure}")
        for error in step.errors:
            print(f"  warning/error: {error}")

    if persistence_failures:
        print()
        print("FAIL: the catalog was read from the provider but NOT persisted.")
        print(f"  {len(persistence_failures)} durable-write failure(s):")
        for failure in persistence_failures:
            print(f"    - {failure}")
        print("  This run is NOT a successful sync; nothing was retired and the")
        print("  storefront still shows the last known state.")
        print("  The usual cause is a database schema that is BEHIND the image:")
        print("    python -m alembic current   # inside the migrate image")
        print("    python -m alembic heads")
        print("    scripts/deploy-production.sh  # applies migrations, then verifies head")
        return 1

    await _print_storefront_readiness("leaseweb")
    return 0


async def hetzner_sync_offers() -> int:
    """Refresh the sellable-offer price book from the Hetzner API.

    READ-ONLY at the provider: it lists locations and the server types offered
    at each one, and writes provider COST + specs into the offer rows. It never
    prices anything for sale and never enables an offer — SYNC != PRICE !=
    ENABLE (see docs/payments/HETZNER.md).
    """
    from cloud_platform.db.session import SessionFactory
    from cloud_platform.providers.hetzner.sync import HetznerCatalogSyncer

    settings = get_settings()
    if not settings.hetzner_api_token:
        print("No Hetzner API token is configured; cannot sync.")
        return 1
    syncer = HetznerCatalogSyncer(
        SessionFactory,
        token=settings.hetzner_api_token,
        base_url=settings.hetzner_api_base_url,
    )
    try:
        result = await syncer.sync_offers()
    finally:
        await syncer.close()

    print(f"offers written: {result.offers_written}")
    for report in result.locations:
        if report.error:
            print(f"  [WARN] {report.location_id}: {report.error}")
        else:
            print(f"  [OK  ] {report.location_id}: {report.products} product(s)")
    for warning in result.warnings:
        print(f"  warning: {warning}")
    print(f"marked provider-unavailable: {result.marked_unavailable}")
    await _print_storefront_readiness("hetzner")
    return 0


async def hetzner_doctor() -> DoctorResult:
    """Read-only Hetzner pre-flight: credentials, catalog and storefront.

    Answers, without mutating anything, WHY the Hetzner storefront is empty:
    is the token configured, does the API answer, which locations does this
    token see, does each location offer server types, and which offer gate is
    still closed. Counts and class names only — never a token, never a price.
    """
    from cloud_platform.db.session import SessionFactory
    from cloud_platform.providers.hetzner.sync import HetznerCatalogSyncer

    settings = get_settings()
    lines: list[str] = ["Hetzner doctor (read-only)"]
    ok = True
    if not settings.hetzner_api_token:
        lines.append("[FAIL] hetzner_api_token is not configured")
        return DoctorResult(False, lines)
    lines.append("[OK  ] hetzner_api_token configured")

    syncer = HetznerCatalogSyncer(
        SessionFactory,
        token=settings.hetzner_api_token,
        base_url=settings.hetzner_api_base_url,
    )
    try:
        try:
            locations, location_errors = await syncer.probe_locations()
        except Exception as exc:  # pragma: no cover - transport guard
            lines.append(f"[FAIL] locations unreachable ({type(exc).__name__})")
            return DoctorResult(False, lines)
        for error in location_errors:
            lines.append(f"[WARN] {error}")
        lines.append(f"[OK  ] locations visible to this credential: {len(locations)}")
        total = 0
        for location_id in locations:
            try:
                items = await syncer.probe_server_types(location_id)
            except Exception as exc:
                lines.append(f"[WARN] {location_id}: list endpoint failed ({type(exc).__name__})")
                ok = False
                continue
            total += len(items)
            lines.append(f"[OK  ] {location_id} products: {len(items)}")
        lines.append(f"Summary: locations={len(locations)} products observed={total}")
    finally:
        await syncer.close()

    try:
        from cloud_platform.modules.offers.repository import SqlAlchemySellableOfferRepository

        rows = [
            row
            for row in await SqlAlchemySellableOfferRepository(SessionFactory).list_all()
            if row.provider_key == "hetzner"
        ]
        counts = _offer_gate_counts(rows)
        lines.append("Storefront gates: " + _visibility_line("customer-visible", counts))
        if not counts["sellable"]:
            ok = False
            lines.append(
                "[WARN] nothing is on sale yet — SYNC != PRICE != ENABLE: "
                "run 'hetzner sync-offers', then 'offers price-book --provider hetzner', "
                "then 'offers enable <offer_id>'"
            )
    except Exception as exc:
        lines.append(f"[WARN] offer rows unavailable ({type(exc).__name__})")
    return DoctorResult(ok, lines)


async def _print_storefront_readiness(provider_key: str) -> None:
    """Say out loud whether the sync produced anything a CUSTOMER can see.

    A sync that stores offers but leaves them unpriced is indistinguishable
    from a broken storefront to the operator, so the gate counts and the exact
    next command are printed here instead of being discovered in Telegram.
    """
    try:
        from cloud_platform.db.session import SessionFactory
        from cloud_platform.modules.offers.repository import (
            SqlAlchemySellableOfferRepository,
        )

        rows = [
            row
            for row in await SqlAlchemySellableOfferRepository(SessionFactory).list_all()
            if row.provider_key == provider_key
        ]
    except Exception as exc:  # pragma: no cover - DB may be unreachable
        print(f"Storefront readiness: unavailable ({type(exc).__name__})")
        return
    counts = _offer_gate_counts(rows)
    print("Storefront readiness:")
    print(f"  stored offers: {len(rows)}")
    print("  " + _visibility_line("customer-visible", counts))
    if counts["sellable"]:
        print(f"  [OK ] {counts['sellable']} offer(s) are on sale now")
        return
    print("  [WARN] NOTHING is on sale — the customer-facing catalog is empty")
    if counts["unpriced"]:
        print("  a provider cost is not a selling price by design; inspect canonical FX pricing:")
        print(
            "    python -m cloud_platform.cli offers normalize-selling-currency "
            "--target USD --dry-run"
        )
        print("    use 'offers price-book' only for a deliberate manual price override")
    print("    python -m cloud_platform.cli offers doctor")


# ---------------------------------------------------------------------------
# leaseweb — read-only VPS API diagnostics (LEASEWEB-VPS-API)
# ---------------------------------------------------------------------------
#
# IMPORTANT: none of these commands can place an order, reinstall, reset a
# password, delete a credential or mutate a snapshot. Billable/destructive
# provider calls only ever flow through the durable checkout -> wallet hold ->
# operation ledger -> worker pipeline (see docs/leaseweb/VPS_API_COVERAGE.md).


def _leaseweb_account_router_factory() -> Any:
    """Import the multi-account factory lazily (keeps CLI start-up fast)."""
    from cloud_platform.providers.leaseweb.accounts import build_leaseweb_account_router

    return build_leaseweb_account_router


def _leaseweb_readonly() -> Any:
    """Import the read-only diagnostics module lazily (keeps CLI start-up fast)."""
    from cloud_platform.providers.leaseweb import diagnostics

    return diagnostics


async def leaseweb_accounts_list() -> int:
    """Operator inventory of the Leaseweb credential accounts (never a key).

    Shows only SAFE metadata: the stable account id, its configured lifecycle,
    the selection priority, this process's authentication verdict and the
    locations the account has been observed to serve. The API key is never
    printed, not even partially.
    """
    settings = get_settings()
    router = _leaseweb_account_router_factory()(settings)
    if router is None:
        if settings.leaseweb_api_key:
            print("Leaseweb uses the deprecated single api_key; no accounts configured.")
            return 0
        print("No Leaseweb credential account is configured.")
        return 1

    locations: dict[str, list[str]] = {}
    try:
        from cloud_platform.db.session import SessionFactory
        from cloud_platform.modules.provider_routes.repository import (
            SqlAlchemyProviderRouteRepository,
        )

        repo = SqlAlchemyProviderRouteRepository(SessionFactory)
        for route in await repo.list_for_provider("leaseweb"):
            if route.state.value != "eligible_available":
                continue
            locations.setdefault(route.credential_account_id, []).append(route.location_id)
    except Exception as exc:  # diagnostics must never raise
        print(f"  (routing table unavailable: {type(exc).__name__})")

    # KEY REMOVAL SAFETY: an account deleted from configuration does not delete
    # the servers and orders it created — they keep its id and fail closed. The
    # operator must be told, with counts, instead of discovering it later from a
    # customer-side failure.
    configured_ids = [account.account_id for account in router.accounts]
    try:
        from cloud_platform.db.session import SessionFactory
        from cloud_platform.modules.provider_routes.dependents import (
            missing_credential_account_report,
        )

        dependents, scan_error = await missing_credential_account_report(
            SessionFactory,
            provider_key="leaseweb",
            configured_account_ids=configured_ids,
        )
    except Exception as exc:  # pragma: no cover - import/session factory guard
        dependents, scan_error = [], type(exc).__name__
    for dependent in dependents:
        print(f"  WARNING: {dependent.message()}")
    if scan_error is not None:
        print(f"  (dependency check unavailable: {scan_error})")

    report = await router.verify_all()
    health = {entry.account_id: entry for entry in report.accounts}
    print(f"Leaseweb overall: {report.status.upper()}")
    print()
    print(f"{'ID':<16}{'enabled':<10}{'state':<11}{'prio':<7}{'auth':<14}locations")
    for account in router.accounts:
        entry = health.get(account.account_id)
        auth = "ok" if entry is not None and entry.ok else (entry.error_class if entry else "-")
        served = ",".join(sorted(locations.get(account.account_id, []))) or "-"
        print(
            f"{account.account_id:<16}{('yes' if account.enabled else 'no'):<10}"
            f"{account.state.value:<11}{account.priority:<7}{auth:<14}{served}"
        )
    print()
    print(f"{len(report.healthy)}/{len(report.accounts)} account(s) authenticated.")
    return 0 if report.healthy else 1


async def leaseweb_accounts_doctor() -> DoctorResult:
    """Per-account pre-flight; the provider is DEGRADED, not down, if one fails."""
    settings = get_settings()
    lines: list[str] = []
    router = _leaseweb_account_router_factory()(settings)
    if router is None:
        return DoctorResult(False, ["[FAIL] no Leaseweb credential account is configured"])

    lines.append(f"[OK ] Leaseweb accounts configured — {len(router.accounts)} configured")

    # KEY REMOVAL SAFETY: resources pinned to an account that is no longer
    # configured fail closed. Report them explicitly (ids and counts only).
    try:
        from cloud_platform.db.session import SessionFactory
        from cloud_platform.modules.provider_routes.dependents import (
            missing_credential_account_report,
        )

        dependents, scan_error = await missing_credential_account_report(
            SessionFactory,
            provider_key="leaseweb",
            configured_account_ids=[account.account_id for account in router.accounts],
        )
    except Exception as exc:  # pragma: no cover - import/session factory guard
        dependents, scan_error = [], type(exc).__name__
    for dependent in dependents:
        lines.append(f"[WARN] {dependent.message()}")
    if scan_error is not None:
        lines.append(f"[WARN] credential-account dependency check unavailable ({scan_error})")

    # Per-credential evidence. Authentication first (read-only, never billable),
    # then catalog discovery: which locations THIS key can actually sell in and
    # how many products each of them reports. Locations are discovered from the
    # provider response, never assumed to be globally available.
    from cloud_platform.providers.leaseweb.ordering_sync import probe_account_catalog

    report = await router.verify_all()
    enabled = [account for account in router.accounts if account.enabled]
    probes: list[Any] = []
    healthy = 0
    for entry in report.accounts:
        account = router.account(entry.account_id)
        if not account.enabled:
            lines.append(
                f"[WARN] credential {account.display_name} is configured but disabled; "
                "nothing was probed and it receives no new traffic"
            )
            continue
        # The authentication probe is ADVISORY. Real, location-scoped catalog
        # reads are the authority on whether this credential actually works:
        # the unscoped probe can be refused while every scoped read succeeds,
        # and reporting that as "invalid credential" is a false negative.
        auth_failed = not entry.ok
        if auth_failed:
            lines.append(
                f"[WARN] credential {account.display_name} authentication probe "
                f"inconclusive ({entry.error_class or 'unknown'}) — verifying with "
                "scoped catalog reads"
            )
        else:
            lines.append(f"[OK ] credential {account.display_name} authenticated")
        if not router.has_provider(entry.account_id):
            if not auth_failed:
                healthy += 1
            continue
        probe = await probe_account_catalog(
            router.client_for(entry.account_id), entry.account_id, seeds=router.locations
        )
        probes.append(probe)
        if not probe.authenticated:
            if auth_failed:
                # Both probes agree the credential is unusable, so say so
                # plainly instead of leaving the account without a verdict.
                lines.append(
                    f"[FAIL] credential {account.display_name} — the authentication "
                    "probe AND the scoped catalog reads failed "
                    f"({probe.error_class or entry.error_class or 'unknown'})"
                )
            else:
                lines.append(
                    f"[WARN] credential {account.display_name} catalog discovery "
                    f"failed ({probe.error_class or 'unknown'})"
                )
            continue
        if auth_failed:
            lines.append(
                f"[OK ] credential {account.display_name} authenticated — scoped "
                f"catalog reads succeed ({probe.location_count} location(s)); the "
                "unscoped probe was not a valid authentication test"
            )
        healthy += 1
        for location_id, count in sorted(probe.products_by_location.items()):
            lines.append(f"[OK ] {location_id} products: {count}")
        for location_id in probe.detail_warnings:
            lines.append(f"[WARN] detail endpoint unavailable for {location_id}")

    if healthy and healthy == len(enabled):
        lines.append(f"[OK ] Leaseweb aggregate catalog — {healthy} usable account(s)")
    elif healthy:
        lines.append(
            f"[WARN] Leaseweb is DEGRADED — {healthy}/{len(enabled)} credential account(s) "
            "usable; the storefront keeps serving the locations that still work"
        )
    else:
        lines.append("[FAIL] Leaseweb is UNAVAILABLE — no credential account authenticated")

    # Summary: how many credentials, how many DISTINCT locations across them,
    # and how many offers those locations currently put on sale. Two keys that
    # both see the same datacenter count it once — the customer sees one place.
    locations = {loc for probe in probes for loc in probe.products_by_location}
    credentials = len(enabled)
    offers = 0
    try:
        from cloud_platform.db.session import SessionFactory
        from cloud_platform.modules.offers.repository import (
            SqlAlchemySellableOfferRepository,
        )

        rows = await SqlAlchemySellableOfferRepository(SessionFactory).list_all()
        offers = sum(1 for row in rows if row.provider_key == "leaseweb" and row.provider_available)
    except Exception as exc:  # pragma: no cover - DB may be unreachable
        lines.append(f"[WARN] stored offer counts unavailable ({type(exc).__name__})")
    lines.append("Summary:")
    lines.append(f"credentials: {credentials}")
    lines.append(f"locations: {len(locations)}")
    lines.append(f"offers: {offers}")

    ok = healthy > 0
    if not ok:
        lines.append(
            "\nAction: check each account's api_key in the server-owned "
            "configuration.toml, then re-run this command."
        )
    return DoctorResult(ok, lines)


async def leaseweb_auth_check() -> int:
    diagnostics = _leaseweb_readonly()
    for line in await diagnostics.auth_check_lines():
        print(line)
    return 0


async def leaseweb_products_list(location: str | None) -> int:
    diagnostics = _leaseweb_readonly()
    for line in await diagnostics.products_list_lines(location):
        print(line)
    return 0


async def leaseweb_product_show(args: argparse.Namespace) -> int:
    diagnostics = _leaseweb_readonly()
    for line in await diagnostics.product_show_lines(
        args.product_id,
        location=args.location,
        operating_system=args.os,
        control_panel=args.control_panel,
        disk_upgrade=args.disk_upgrade,
        contract_term=args.contract_term,
        billing_cycle=args.billing_cycle,
        service_level_agreement=args.sla,
    ):
        print(line)
    return 0


async def leaseweb_orders_list(limit: int) -> int:
    diagnostics = _leaseweb_readonly()
    for line in await diagnostics.orders_list_lines(limit=limit):
        print(line)
    return 0


async def leaseweb_order_show(order_id: str) -> int:
    diagnostics = _leaseweb_readonly()
    for line in await diagnostics.order_show_lines(order_id):
        print(line)
    return 0


async def leaseweb_vps_list() -> int:
    diagnostics = _leaseweb_readonly()
    for line in await diagnostics.vps_list_lines():
        print(line)
    return 0


async def leaseweb_vps_show(vps_id: str) -> int:
    diagnostics = _leaseweb_readonly()
    for line in await diagnostics.vps_show_lines(vps_id):
        print(line)
    return 0


async def leaseweb_vps_ips(vps_id: str) -> int:
    diagnostics = _leaseweb_readonly()
    for line in await diagnostics.vps_ips_lines(vps_id):
        print(line)
    return 0


async def leaseweb_vps_metrics(args: argparse.Namespace) -> int:
    diagnostics = _leaseweb_readonly()
    for line in await diagnostics.vps_metrics_lines(
        args.vps_id,
        from_=args.date_from,
        to=args.date_to,
        granularity=args.granularity,
        aggregation=args.aggregation,
    ):
        print(line)
    return 0


async def leaseweb_vps_snapshots(vps_id: str) -> int:
    diagnostics = _leaseweb_readonly()
    for line in await diagnostics.vps_snapshots_lines(vps_id):
        print(line)
    return 0


async def leaseweb_vps_monitoring(vps_id: str) -> int:
    diagnostics = _leaseweb_readonly()
    for line in await diagnostics.vps_monitoring_lines(vps_id):
        print(line)
    return 0


def leaseweb_coverage() -> int:
    diagnostics = _leaseweb_readonly()
    for line in diagnostics.coverage_lines():
        print(line)
    return 0


# ---------------------------------------------------------------------------
# offers / users / wallet / orders / renewals
# ---------------------------------------------------------------------------


async def offers_list(include_all: bool) -> int:
    from cloud_platform.db.session import SessionFactory
    from cloud_platform.modules.offers.repository import SqlAlchemySellableOfferRepository

    repo = SqlAlchemySellableOfferRepository(SessionFactory)
    offers = await repo.list_all() if include_all else await repo.list_sellable("leaseweb")
    if not offers:
        print("no offers" + ("" if include_all else " — sync, then price (offers price-book)"))
        return 0
    # One definition of visibility for every diagnostic: the SAME first
    # blocking gate the storefront enforces, not a second opinion.
    from cloud_platform.modules.offers.domain import (
        GATE_CURRENCY,
        GATE_DEPRECATED,
        GATE_DISABLED,
        GATE_OPERATOR_DISABLED,
        GATE_PRICING_PENDING,
        GATE_PRICING_PROVENANCE,
        GATE_PROVIDER_UNAVAILABLE,
        GATE_UNPRICED,
        blocking_gate,
    )

    flags = {
        None: "SALE",
        GATE_DISABLED: "off",
        GATE_UNPRICED: "no-price",
        GATE_PROVIDER_UNAVAILABLE: "unavail",
        GATE_CURRENCY: "currency",
        GATE_OPERATOR_DISABLED: "operator",
        GATE_PRICING_PENDING: "pending",
        GATE_PRICING_PROVENANCE: "provenance",
        GATE_DEPRECATED: "deprecated",
    }
    catalog_currency = get_settings().fx_catalog_pricing_currency
    for offer in sorted(offers, key=lambda o: (o.location_id, o.product_id)):
        flag = flags[blocking_gate(offer, catalog_currency)]
        print(
            f"{offer.id}  {flag:8s}  {offer.location_id:8s} {offer.product_id:12s} "
            f"{offer.name:24s} cost={offer.provider_cost_minor} {offer.provider_cost_currency} "
            f"price={offer.selling_price_minor} {offer.selling_currency}"
        )
    return 0


def _offer_gate_counts(offers: list[Any], catalog_currency: str | None = None) -> dict[str, int]:
    """Count offers per blocking gate for one provider (pure domain helper)."""
    from cloud_platform.modules.offers.domain import visibility_summary

    return visibility_summary(offers, catalog_currency)


async def offers_doctor() -> DoctorResult:
    """Read-only: WHY the storefront is empty, end to end.

    An offer reaches a customer only through five gates: the provider must be
    configured with a market, enabled, and have an ORDERING adapter; and the
    offer row itself must be provider-reported, operator-enabled and
    explicitly priced. "Nothing is for sale" is therefore not diagnosable
    from the offer table alone, so this walks the whole funnel and names the
    failing step with counts. Read-only; never prints a credential.
    """
    lines: list[str] = []
    ok = True
    catalog_currency = get_settings().fx_catalog_pricing_currency

    def fail(message: str) -> None:
        nonlocal ok
        ok = False
        lines.append(f"[FAIL] {message}")

    try:
        from cloud_platform.db.session import SessionFactory
        from cloud_platform.modules.offers.repository import (
            SqlAlchemySellableOfferRepository,
        )

        rows = await SqlAlchemySellableOfferRepository(SessionFactory).list_all()
    except Exception as exc:
        # Only the exception CLASS is ever printed: a configuration
        # ValidationError embeds the offending input, which can carry
        # credentials, so its message and errors must never reach the output.
        return DoctorResult(
            False,
            [
                f"[FAIL] the offer price book could not be read "
                f"({type(exc).__name__}) — check configuration.toml and the database"
            ],
        )

    by_provider: dict[str, list[Any]] = {}
    for row in rows:
        by_provider.setdefault(row.provider_key, []).append(row)

    # 1) configuration gates: a provider without a market is INVISIBLE even
    # with a fully priced catalog, and a provider without an ordering adapter
    # is shown as "soon" and cannot be entered. The container is inspected
    # read-only and closed again; a container that cannot be built must not
    # hide the offer-gate evidence below.
    from cloud_platform.core.container import create_container
    from cloud_platform.modules.markets.domain import MARKET_ORDER
    from cloud_platform.providers.base import ordering_support_of

    catalog: Any = None
    providers: dict[str, Any] = {}
    registry: Any = None
    container = None
    try:
        container = create_container()
        await container.initialize()
        catalog = container.market_catalog()
        registry = container.provider_registry
        for key in registry.keys():
            providers[key] = registry.get(key)
    except Exception as exc:  # pragma: no cover - depends on local settings
        lines.append(f"[WARN] provider registry unavailable ({type(exc).__name__})")
    finally:
        if container is not None:
            await container.close()

    markets_shown: dict[str, list[str]] = {}
    for provider_key in sorted(set(providers) | set(by_provider)):
        market = catalog.market_of(provider_key) if catalog is not None else None
        enabled = catalog.is_enabled(provider_key) if catalog is not None else True
        provider = providers.get(provider_key)
        ordering = provider is not None and ordering_support_of(provider) is not None
        if market is None:
            fail(
                f"provider {provider_key} has no market configured — it never "
                'appears in the store (set market = "iran" or "foreign")'
            )
        elif not enabled:
            lines.append(f"[WARN] provider {provider_key} is disabled in configuration")
        elif not ordering:
            lines.append(
                f"[WARN] provider {provider_key} has no ordering adapter — the "
                "storefront can only show it as unavailable"
            )
        else:
            markets_shown.setdefault(market.value, []).append(provider_key)
        counts = _offer_gate_counts(
            by_provider.get(provider_key, []), catalog_currency=catalog_currency
        )
        lines.append(
            f"[{'OK ' if counts['sellable'] else 'WARN'}] provider {provider_key} "
            f"(stored={len(by_provider.get(provider_key, []))}): "
            + _visibility_line("offers", counts)
        )
        if counts["unpriced"] and not counts["sellable"]:
            lines.append(
                "       every offer is missing a CUSTOMER PRICE — a provider "
                "cost is not a selling price by design"
            )
            lines.append(
                f"       Action: run the configured automatic pricing policy or "
                f"'offers normalize-selling-currency --target {catalog_currency} --dry-run' "
                f"then re-run catalog sync; never relabel native prices"
            )
        if (
            not counts["sellable"]
            and (counts.get("selling_currency") or counts.get("pricing_provenance"))
            and not counts["unpriced"]
        ):
            lines.append(
                "       priced rows are blocked on currency/provenance — a manual "
                "non-USD price is never relabelled into the catalog currency"
            )
            lines.append(
                f"       Action: 'offers normalize-selling-currency "
                f"--target {catalog_currency} --dry-run' to convert deliberately"
            )

    # 2) what the bot would actually render for each market
    for market in MARKET_ORDER:
        market_providers = markets_shown.get(market.value, [])
        has_sellable = [
            key
            for key in market_providers
            if _offer_gate_counts(by_provider.get(key, []), catalog_currency=catalog_currency)[
                "sellable"
            ]
        ]
        if has_sellable:
            lines.append(f"[OK ] market {market.value}: {', '.join(sorted(has_sellable))}")
        elif market_providers:
            fail(
                f"market {market.value} would show NO provider: "
                f"{', '.join(sorted(market_providers))} configured but none has a "
                "sellable offer"
            )
        else:
            lines.append(f"[WARN] market {market.value}: no provider configured")

    # 3) credential provenance. A provider served by several credential accounts
    #    records WHICH key supplied each observation; offers written before that
    #    (or repaired forward by a migration) can still carry the legacy account
    #    id. A refresh is the authority on provenance and a diagnostic must not
    #    rewrite it, so a stale pin is REPORTED with the command that fixes it.
    lines.extend(await _credential_provenance_notes(by_provider, providers, registry))

    if not rows:
        fail(
            "the offer price book is EMPTY — no catalog sync has ever landed; "
            "run: python -m cloud_platform.cli leaseweb sync-offers"
        )
    return DoctorResult(ok, lines)


async def _credential_provenance_notes(
    by_provider: dict[str, list[Any]],
    providers: dict[str, Any],
    registry: Any | None,
) -> list[str]:
    """Report available offers whose supplying credential account is unproven.

    Read-only. It never rewrites a row: the catalog sync owns provenance. Silent
    for providers that are not served by credential accounts (they are routed
    logically and have no account to record), and for offers that already name
    the account that supplied them.
    """
    from cloud_platform.providers.routing import DEFAULT_CREDENTIAL_ACCOUNT

    notes: list[str] = []
    if registry is None:
        return notes
    for provider_key in sorted(by_provider):
        try:
            views = registry.accounts(provider_key)
        except Exception:
            continue
        real_accounts = {
            str(view.account_id)
            for view in views
            if str(view.account_id) != DEFAULT_CREDENTIAL_ACCOUNT
        }
        if not real_accounts:
            # A single legacy credential IS account ``default``: not stale.
            continue
        stale = [
            row
            for row in by_provider[provider_key]
            if row.provider_available
            and str(getattr(row, "provider_account_id", None) or DEFAULT_CREDENTIAL_ACCOUNT)
            == DEFAULT_CREDENTIAL_ACCOUNT
        ]
        if not stale:
            continue
        locations = sorted({str(row.location_id) for row in stale})
        routed: set[str] = set()
        try:
            from cloud_platform.db.session import SessionFactory
            from cloud_platform.modules.provider_routes.repository import (
                SqlAlchemyProviderRouteRepository,
            )

            routes = await SqlAlchemyProviderRouteRepository(SessionFactory).list_for_provider(
                provider_key
            )
            routed = {
                str(route.location_id)
                for route in routes
                if str(route.credential_account_id) != DEFAULT_CREDENTIAL_ACCOUNT
            }
        except Exception as exc:
            notes.append(
                f"[WARN] {provider_key}: credential routes unreadable "
                f"({type(exc).__name__}) — provenance of {len(stale)} offer(s) "
                "cannot be confirmed"
            )
        known = [location for location in locations if location in routed]
        evidence = (
            f" — a supplying account is already known for {len(known)} of them" if known else ""
        )
        notes.append(
            f"[WARN] {provider_key}: {len(stale)} available offer(s) still carry the "
            f"legacy credential provenance ('{DEFAULT_CREDENTIAL_ACCOUNT}') across "
            f"{len(locations)} location(s){evidence}; run 'python -m "
            f"cloud_platform.cli {provider_key} sync-offers' to record which "
            "credential account supplies each offer — a refresh updates provider "
            "cost, never the selling price or the enable state"
        )
    return notes


def _visibility_line(label: str, counts: dict[str, int]) -> str:
    """One human line of gate counts (used by the catalog diagnostics)."""
    return (
        f"{label}: sellable={counts['sellable']} "
        f"unpriced={counts['unpriced']} disabled={counts['disabled']} "
        f"provider-unavailable={counts['provider_unavailable']} "
        f"operator-disabled={counts.get('operator_disabled', 0)} "
        f"pricing-pending={counts.get('pricing_pending', 0)} "
        f"pricing-provenance={counts.get('pricing_provenance', 0)} "
        f"deprecated={counts.get('deprecated', 0)} "
        f"selling-currency={counts.get('selling_currency', 0)}"
    )


async def offers_preview(market: str | None) -> int:
    """Print the customer catalog EXACTLY as the bot assembles it.

    Read-only and presentation-only: it walks the same view service the
    Telegram UI walks (market -> provider -> product card -> locations), so
    what an operator sees here is what a customer sees — including an empty
    catalog, which is why the gate counts are printed alongside it.
    """
    from cloud_platform.core.container import create_container
    from cloud_platform.modules.checkout.service import OfferUnavailableError
    from cloud_platform.modules.fx.formatting import format_minor
    from cloud_platform.modules.markets.domain import MARKET_ORDER

    wanted = [market] if market else [m.value for m in MARKET_ORDER]
    container = create_container()
    printed_any = False
    try:
        await container.initialize()
        view = container.offer_catalog_view_service()
        for entry in view.markets_screen():
            if entry.market not in wanted:
                continue
            print(f"market {entry.market}")
            try:
                providers, _back = await view.providers_screen(entry.market)
            except OfferUnavailableError as exc:
                print(f"  (unavailable: {exc})")
                continue
            if not providers:
                print("  (no provider has a sellable offer)")
                continue
            for provider in providers:
                state = "buyable" if provider.select_callback else "shown as soon"
                print(
                    f"  {provider.display_name} ({provider.provider_key}) — {state}, "
                    f"{provider.offer_count} sellable offer(s)"
                )
                if not provider.select_callback:
                    continue
                try:
                    groups, _b, _c = await view.products_screen(provider.provider_key)
                except OfferUnavailableError:
                    continue
                for group in groups:
                    printed_any = True
                    price = format_minor(group.monthly_price_minor, group.currency)
                    print(
                        f"    {group.name} — {price}/month — "
                        f"{group.vcpu} vCPU / {group.ram_gb} GB RAM / "
                        f"{group.disk_gb} GB disk"
                    )
                    locations, _lb, _lc = await view.product_locations_screen(
                        provider.provider_key,
                        group.product_id,
                        group.monthly_price_minor,
                        group.currency,
                    )
                    for location in locations:
                        line_price = format_minor(location.monthly_price_minor, location.currency)
                        print(
                            f"      {location.name} {location.location_id} "
                            f"({location.country_code or '??'}) — {line_price}"
                        )
    finally:
        await container.close()
    if not printed_any:
        print("no product card is visible to a customer — run: offers doctor")
        return 1
    return 0


async def catalog_auto_sync_run() -> int:
    """Run ONE complete automatic catalog refresh now (the periodic job).

    The same advisory-locked, provider-isolated coordinator pass the worker
    cron runs: official read-only APIs refresh provider costs/availability, the
    markup policy reprices auto-priced rows, and eligible rows are published.
    Unlike the 120-second generic worker job timeout this is bounded by the
    dedicated catalog budget, which is why it is the supported way to repair a
    catalog whose provider facts are stale (a hand-rolled timeout that cancels
    the run mid-walk is what left a whole storefront unpriced).

    Exit 0 only when the refresh really ran and every provider finished without
    errors; exit 1 when it was disabled, unconfigured, cancelled at its
    budget, or a provider reported errors. Availability itself is asserted by
    ``offers readiness``.
    """
    import asyncio

    from cloud_platform.core.config import get_settings
    from cloud_platform.worker.settings import (
        catalog_auto_sync_timeout,
        run_catalog_auto_sync_once,
    )

    settings = get_settings()
    if not settings.storefront_catalog_sync_enabled:
        print("catalog auto-sync is disabled by configuration; nothing to run")
        return 1
    budget = catalog_auto_sync_timeout()
    print(f"running one complete catalog refresh (timeout {budget}s)")
    try:
        async with asyncio.timeout(budget):
            report = await run_catalog_auto_sync_once()
    except TimeoutError:
        print(
            f"catalog refresh did NOT finish within {budget}s and was cancelled; "
            "the advisory lock is released and the next run resumes"
        )
        return 1
    except Exception as exc:
        print(f"catalog refresh failed ({type(exc).__name__}); see the worker log")
        return 1
    if report is None:
        print(
            "catalog refresh did not run: it is disabled, no provider is configured, "
            "or another run holds the catalog advisory lock"
        )
        return 1
    for provider in report.providers:
        print(
            f"  {provider.provider_key}: ok={provider.ok} "
            f"discovered={provider.discovered} persisted={provider.persisted} "
            f"prices={provider.prices_updated} published={provider.published} "
            f"retired={provider.retired} warnings={len(provider.warnings)} "
            f"errors={len(provider.errors)}"
        )
    degraded = [provider for provider in report.providers if not provider.ok or provider.errors]
    if degraded:
        print(
            "catalog refresh completed with provider errors: "
            + ", ".join(provider.provider_key for provider in degraded)
        )
        return 1
    print("catalog refresh completed")
    return 0


def _toml_leaf_paths(document: dict[str, Any]) -> list[tuple[str, ...]]:
    """Every leaf key path of a parsed configuration document."""
    leaves: list[tuple[str, ...]] = []

    def walk(node: dict[str, Any], prefix: tuple[str, ...]) -> None:
        for key, value in node.items():
            path = (*prefix, key)
            if isinstance(value, dict):
                walk(value, path)
            else:
                leaves.append(path)

    walk(document, ())
    return leaves


def _toml_leaf_value(document: dict[str, Any], path: tuple[str, ...]) -> Any:
    """Value at a leaf path (``None`` when the path does not exist)."""
    node: Any = document
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _toml_has(document: dict[str, Any], path: tuple[str, ...]) -> bool:
    node: Any = document
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


async def config_doctor() -> int:
    """Read-only: compare the real configuration with the canonical contract.

    Reports configuration drift in both directions WITHOUT ever printing a
    value — only key paths:

    * keys the loader supports but the file does not set (the Settings default
      applies; a model-required key is a hard failure);
    * keys the file sets that the loader does not know (silently ignored today,
      which is how a typo or a removed setting stays unnoticed);
    * the documented ``CHANGE_ME`` placeholders still present in a
      ``production`` file, which is an invalid configuration rather than drift;
    * configured customer-facing labels (every ``display_name``) that are
      obviously corrupted — terminal mojibake such as ``"????????"`` is never
      a customer-facing name, so it fails the file.

    It never rewrites anything: the operator merges the missing keys and
    uploads the file. Exit 1 for an unreadable/invalid file, a missing required
    key, an unreplaced placeholder in production or a corrupted label;
    otherwise 0.
    """
    import tomllib

    from cloud_platform.core.config import _TOML_FIELDS as toml_contract
    from cloud_platform.core.config import (
        TOML_LEGACY_ALIAS_KEYS,
        ConfigFileError,
        Settings,
        looks_corrupted_label,
        resolve_config_file,
    )

    try:
        path = resolve_config_file()
    except ConfigFileError as exc:
        print(f"error: {exc}")
        print("config doctor: FAIL")
        return 1
    if path is None:
        print(
            "no configuration file found (CLOUD_PLATFORM_CONFIG_FILE, "
            "./configuration.toml, /etc/cloud-server-seller/configuration.toml); "
            "built-in defaults are in effect"
        )
        print("config doctor: FAIL")
        return 1
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        print(f"error: {path} is not readable valid TOML ({type(exc).__name__})")
        print("config doctor: FAIL")
        return 1
    print(f"configuration contract: {path}")

    missing_required: list[str] = []
    missing_defaulted: list[str] = []
    for section_key, field_name in toml_contract.items():
        if section_key in TOML_LEGACY_ALIAS_KEYS or _toml_has(document, section_key):
            continue
        name = ".".join(section_key)
        model_field = Settings.model_fields.get(field_name)
        if model_field is not None and model_field.is_required():
            missing_required.append(name)
        else:
            missing_defaulted.append(name)

    supported = set(toml_contract)
    unknown: list[str] = []
    for leaf in _toml_leaf_paths(document):
        if leaf in supported:
            continue
        # Schema-driven subtrees: providers (accounts/families/nested sections)
        # and the per-provider automatic pricing policies.
        if leaf[0] == "providers" or leaf[:2] == ("storefront", "pricing"):
            continue
        unknown.append(".".join(leaf))

    environment = _toml_leaf_value(document, ("app", "environment"))
    production = str(environment or "").strip().lower() == "production"
    placeholders = [
        ".".join(leaf)
        for leaf in _toml_leaf_paths(document)
        if _toml_leaf_value(document, leaf) == "CHANGE_ME"
    ]
    # Customer-facing names only. The value is never printed — the key path
    # alone identifies the operator's paste accident.
    corrupted_labels = [
        ".".join(leaf)
        for leaf in _toml_leaf_paths(document)
        if leaf[-1] == "display_name" and looks_corrupted_label(_toml_leaf_value(document, leaf))
    ]

    if missing_required:
        print("missing REQUIRED keys:")
        for name in sorted(missing_required):
            print(f"  {name}")
    if missing_defaulted:
        print("missing keys (the Settings default applies):")
        for name in sorted(missing_defaulted):
            print(f"  {name}")
    if unknown:
        print("unknown keys (ignored by the loader; typo or removed setting):")
        for name in sorted(unknown):
            print(f"  {name}")
    if placeholders:
        label = (
            "placeholders that must be replaced"
            if production
            else "placeholders (fine outside production)"
        )
        print(f"{label}:")
        for name in sorted(placeholders):
            print(f"  {name}")
    if corrupted_labels:
        print("customer-facing labels that look corrupted (never shown to customers):")
        for name in sorted(corrupted_labels):
            print(f"  [FAIL] {name}: customer-facing label appears corrupted")
    if not (missing_required or missing_defaulted or unknown or placeholders or corrupted_labels):
        print("every supported key is present, no unknown keys, no placeholders")

    unusable = (
        bool(missing_required) or (production and bool(placeholders)) or bool(corrupted_labels)
    )
    print(f"config doctor: {'FAIL' if unusable else 'OK'}")
    return 1 if unusable else 0


async def catalog_auto_sync_doctor() -> int:
    """Read-only: automatic catalog sync configuration and per-provider status.

    Shows whether the coordinator is enabled, at which interval, which
    pricing/publication policy each provider has, whether its credential is
    configured (presence only — never a secret), the last attempt/success
    with the last run's counters, and how many offers are sellable right
    now. After the one-time server configuration, plan changes need no
    manual sync-offers / price-book / enable commands.
    """
    from cloud_platform.core.config import get_settings
    from cloud_platform.db.session import SessionFactory
    from cloud_platform.modules.offers.auto_sync import pricing_policies_from_settings
    from cloud_platform.modules.offers.domain import (
        has_valid_pricing_provenance,
        is_sellable_in_currency,
    )
    from cloud_platform.modules.offers.repository import (
        SqlAlchemyCatalogSyncStateRepository,
        SqlAlchemySellableOfferRepository,
    )

    settings = get_settings()
    policies = pricing_policies_from_settings(settings)
    try:
        states = {
            state.provider_key: state
            for state in await SqlAlchemyCatalogSyncStateRepository(SessionFactory).list_all()
        }
        rows = await SqlAlchemySellableOfferRepository(SessionFactory).list_all()
    except Exception as exc:
        print(f"error: catalog state unreadable ({type(exc).__name__})")
        return 1

    catalog_currency = settings.fx_catalog_pricing_currency.strip().upper()
    sellable: dict[str, int] = {}
    stored: dict[str, int] = {}
    for row in rows:
        stored[row.provider_key] = stored.get(row.provider_key, 0) + 1
        if is_sellable_in_currency(row, catalog_currency):
            sellable[row.provider_key] = sellable.get(row.provider_key, 0) + 1

    # Leaseweb billing split (monthly VPS vs hourly Cloud share one
    # provider key in the offer table but have SEPARATE sync states and
    # pricing policies — never one opaque "leaseweb" status). Rows without
    # a billing_model (older test doubles) count as monthly.
    def _billing_of(row: Any) -> str:
        return str(
            getattr(row, "billing_model", "prepaid_monthly_fixed") or "prepaid_monthly_fixed"
        )

    leaseweb_monthly_stored = sum(
        1
        for row in rows
        if row.provider_key == "leaseweb" and _billing_of(row) == "prepaid_monthly_fixed"
    )
    leaseweb_monthly_sellable = sum(
        1
        for row in rows
        if row.provider_key == "leaseweb"
        and _billing_of(row) == "prepaid_monthly_fixed"
        and is_sellable_in_currency(row, catalog_currency)
    )
    leaseweb_hourly_stored = sum(
        1 for row in rows if row.provider_key == "leaseweb" and _billing_of(row) == "hourly"
    )
    leaseweb_hourly_sellable = sum(
        1
        for row in rows
        if row.provider_key == "leaseweb"
        and _billing_of(row) == "hourly"
        and is_sellable_in_currency(row, catalog_currency)
    )

    leaseweb_credential = bool(settings.leaseweb_api_key)
    try:
        from cloud_platform.providers.leaseweb.accounts import build_leaseweb_account_router

        router = build_leaseweb_account_router(settings)
        if router is not None:
            leaseweb_credential = True
    except Exception:
        pass
    credentials = {
        "leaseweb": leaseweb_credential,
        "hetzner": bool(settings.hetzner_api_token),
    }

    toggle = "enabled" if settings.storefront_catalog_sync_enabled else "disabled"
    print(f"catalog auto-sync: {toggle}")
    print(f"interval: {settings.storefront_catalog_sync_interval_seconds}s")
    catalog_currency = settings.fx_catalog_pricing_currency.strip().upper()
    print(
        f"catalog currency: {catalog_currency}; global FX: "
        f"{'enabled' if settings.fx_enabled and settings.fx_global_enabled else 'disabled'}"
    )
    foreign = [
        row
        for row in rows
        if row.provider_cost_currency.strip().upper() not in {"IRT", "IRR"}
        and row.provider_cost_currency.strip().upper() != catalog_currency
    ]
    pending = sum(bool(row.pricing_metadata.get("fx_repricing_pending")) for row in rows)
    provenance = sum(has_valid_pricing_provenance(row, catalog_currency) for row in rows)
    print(
        f"pricing audit: foreign={len(foreign)} pending={pending} "
        f"valid-provenance={provenance}/{len(rows)}"
    )
    print("")
    loop_keys = set(stored) | set(states) | set(policies) | set(credentials)
    # Billing-suffixed sync-state/policy keys ("<provider>.hourly") alias
    # their provider, which the dedicated section renders: fold them so the
    # generic loop never invents a fake "leaseweb.hourly" provider entry.
    folded: set[str] = set()
    for key in loop_keys:
        base, dot, _suffix = key.partition(".")
        folded.add(base if dot and base in loop_keys else key)
    for provider_key in sorted(folded):
        # Leaseweb is reported as TWO product lines (monthly + hourly),
        # never one opaque status: the offer table shares the key but the
        # sync states ("leaseweb" vs "leaseweb.hourly") and pricing
        # policies are distinct.
        if provider_key == "leaseweb":
            _print_leaseweb_line(
                settings,
                policies,
                states,
                enabled=settings.providers_enabled.get("leaseweb", True),
                credential=bool(credentials.get("leaseweb")),
                billing="monthly",
                state_key="leaseweb",
                stored=leaseweb_monthly_stored,
                sellable=leaseweb_monthly_sellable,
            )
            _print_leaseweb_line(
                settings,
                policies,
                states,
                enabled=settings.providers_enabled.get("leaseweb", True),
                credential=bool(credentials.get("leaseweb")),
                billing="hourly",
                state_key="leaseweb.hourly",
                stored=leaseweb_hourly_stored,
                sellable=leaseweb_hourly_sellable,
            )
            continue
        enabled = settings.providers_enabled.get(provider_key, True)
        policy = policies.get(provider_key)
        state = states.get(provider_key)
        print(f"{provider_key}:")
        print(f"  enabled: {'yes' if enabled else 'no'}")
        print(f"  credential configured: {'yes' if credentials.get(provider_key) else 'no'}")
        if policy is not None:
            print(
                f"  auto pricing: {policy.mode} {policy.markup_percent}%\n"
                f"  auto publish: {'yes' if policy.auto_publish else 'no'}"
            )
        else:
            print("  auto pricing: not configured (costs refresh only)")
        if state is not None and state.last_attempted_at is not None:
            print(f"  last attempt: {state.last_attempted_at.isoformat()}")
            print(
                f"  last success: "
                f"{state.last_success_at.isoformat() if state.last_success_at else 'never'}"
            )
            print(
                f"  last run: discovered={state.discovered} persisted={state.persisted} "
                f"prices={state.prices_updated} published={state.published} "
                f"retired={state.retired}"
            )
            for warning in state.warnings[:5]:
                print(f"    warning: {warning}")
            for error in state.errors[:5]:
                print(f"    error: {error}")
        else:
            print("  last success: never (no coordinator run recorded)")
        print(f"  sellable: {sellable.get(provider_key, 0)} (stored={stored.get(provider_key, 0)})")
        print("")
    return 0


def _print_leaseweb_line(
    settings: Any,
    policies: dict[str, Any],
    states: dict[str, Any],
    *,
    enabled: bool,
    credential: bool,
    billing: str,
    state_key: str,
    stored: int,
    sellable: int,
) -> None:
    """One leaseweb product line (monthly VPS vs hourly Cloud), read-only."""
    # Family policy first ("leaseweb.hourly"), then provider-wide
    # ("leaseweb"); absent means costs-only. The lookup mirrors the
    # coordinator's _policy_for so the doctor never disagrees with it.
    if billing == "hourly":
        policy = policies.get("leaseweb.hourly") or policies.get("leaseweb")
        policy_source = (
            "leaseweb.hourly"
            if "leaseweb.hourly" in policies
            else ("leaseweb" if "leaseweb" in policies else None)
        )
        label = "leaseweb.hourly: (hourly)"
    else:
        policy = policies.get("leaseweb")
        policy_source = "leaseweb" if "leaseweb" in policies else None
        # Bare "leaseweb:" prefix stays greppable for existing runbooks/tests.
        label = "leaseweb: (monthly)"
    # Back-compat: the bare "leaseweb:" prefix stays greppable for the
    # monthly line (existing operator runbooks grep for it).
    print(label)
    print(f"  enabled: {'yes' if enabled else 'no'}")
    print(f"  credential configured: {'yes' if credential else 'no'}")
    if policy is not None:
        print(
            f"  auto pricing: {policy.mode} {policy.markup_percent}% "
            f"(source: [{policy_source}])\n"
            f"  auto publish: {'yes' if policy.auto_publish else 'no'}"
        )
    else:
        print("  auto pricing: not configured (costs refresh only)")
        if billing == "hourly":
            print('  action: add [storefront.pricing."leaseweb.hourly"]')
    state = states.get(state_key)
    # Older deployments recorded hourly under "leaseweb": fall back to it
    # for the hourly line rather than reporting "never" incorrectly.
    if state is None and billing == "hourly":
        state = states.get("leaseweb")
    if state is not None and state.last_attempted_at is not None:
        print(f"  last attempt: {state.last_attempted_at.isoformat()}")
        print(
            f"  last success: "
            f"{state.last_success_at.isoformat() if state.last_success_at else 'never'}"
        )
        print(
            f"  last run: discovered={state.discovered} persisted={state.persisted} "
            f"prices={state.prices_updated} published={state.published} "
            f"retired={state.retired}"
        )
        for warning in state.warnings[:5]:
            print(f"    warning: {warning}")
        for error in state.errors[:5]:
            print(f"    error: {error}")
    else:
        print("  last success: never (no coordinator run recorded)")
    print(f"  sellable: {sellable} (stored={stored})")
    print("")


def _cloud_unavailable_reason(capability: Any) -> str:
    """One-line classification of an inaccessible Public Cloud account.

    Distinguishes, without secrets: auth denied (error_class), a failed
    regions read (exception class), a failed instance-type read
    (``RegionError``), an endpoint that returned zero regions, an
    unrecognized regions response (raw items, nothing parsed), regions with
    uniformly empty instance-type lists, types present but unpriced, and an
    unrecognized types response. Read-only diagnostics only.
    """
    error_class = getattr(capability, "error_class", None)
    if error_class == "AuthenticationError":
        return "auth denied for this account"
    if error_class == "RegionError":
        return "instance-type request failed (transient/unknown)"
    if error_class:
        return f"regions request failed ({error_class})"
    regions_raw = int(getattr(capability, "regions_raw_items", 0) or 0)
    regions_seen = int(getattr(capability, "regions_seen", 0) or 0)
    types_raw = int(getattr(capability, "types_raw_items", 0) or 0)
    types_priced = int(getattr(capability, "types_priced_items", 0) or 0)
    types_currency = getattr(capability, "types_currency", None)
    if regions_raw == 0:
        return "regions endpoint returned zero regions"
    if regions_seen == 0:
        return f"regions response not recognized ({regions_raw} raw item(s), 0 parsed)"
    if types_raw == 0:
        return "regions exist but instanceTypes are all empty"
    if not types_currency:
        return "response has no usable _metadata.currency; pricing fail-closed"
    if types_priced == 0:
        return "types present but none carry a usable hourly price/currency"
    parsed_types = sum(count for _region_id, count in (getattr(capability, "regions", ()) or ()))
    if parsed_types == 0:
        return f"instance-type responses not recognized ({types_raw} raw item(s), 0 parsed)"
    return "no usable Cloud catalog"


async def _hourly_cloud_provider_or_error() -> Any:
    """Hourly cloud adapter from settings, or an explanatory failure.

    Legacy single-adapter path (kept for the catalog subcommand): the
    account-aware doctor and sync use the cloud account router instead so
    no single arbitrary key is assumed to own Public Cloud.
    """
    from cloud_platform.core.config import get_settings
    from cloud_platform.providers.leaseweb.cloud import hourly_provider_from_settings

    provider = hourly_provider_from_settings(get_settings())
    if provider is None:
        print("error: leaseweb hourly cloud has no credential configured")
        return None
    return provider


async def leaseweb_cloud_doctor() -> int:
    """Read-only multi-account hourly Cloud diagnostics (never mutates).

    Reports, without secret values:

    - credential accounts examined (ids only, deterministic priority order)
    - which account(s) can access Public Cloud (regions + type counts)
    - region count per account and instance-type count per region
    - image count/read status per region (from the owning account)
    - catalog sync status (last attempt/success + last run counters)
    - hourly offers stored, split by every sellability gate
      (provider_available, priced, auto-priced/manual, enabled,
      operator_disabled, sellable)
    - pricing-policy source (``leaseweb.hourly`` vs ``leaseweb`` vs missing,
      with the exact non-secret TOML block when missing)
    - last sync result and last successful sync

    A 401/auth failure for one account never invalidates another; an
    account with no Public Cloud entitlement is ineligible (not unknown);
    a transient timeout/5xx is reported as unknown and never retires
    inventory here (this command never writes).
    """
    from cloud_platform.core.config import get_settings
    from cloud_platform.modules.offers.auto_sync import pricing_policies_from_settings
    from cloud_platform.modules.offers.domain import is_sellable_in_currency

    settings = get_settings()
    try:
        from cloud_platform.providers.leaseweb.cloud_accounts import (
            build_cloud_account_router,
        )

        cloud_router = build_cloud_account_router(settings)
    except Exception as exc:
        print(f"error: cloud account router unavailable ({type(exc).__name__})")
        return 1
    if cloud_router is None:
        print("error: leaseweb hourly cloud has no credential configured")
        print("action: configure [providers.leaseweb.accounts.*] api_key values,")
        print("  or the deprecated [providers.leaseweb] api_key, then re-run")
        return 1

    accounts = list(cloud_router.accounts)
    print(f"leaseweb cloud doctor (read-only, {len(accounts)} credential account(s))")
    print(f"credential accounts examined: {', '.join(a.account_id for a in accounts) or '-'}")
    # Durable NEW-ORDER capacity state: a credential can be fully
    # authenticated, able to list regions and types, and still be unable to
    # accept a new instance because its provider limit was reached
    # (Leaseweb PC-2031). That is reported here so an operator never has to
    # infer it from a customer-side failure.
    capacity_records: dict[str, Any] = {}
    capacity_states = _cloud_capacity_repository()
    if capacity_states is None:
        print("capacity state: unreadable (store unavailable)")
    else:
        try:
            for record in await capacity_states.list_for_provider("leaseweb"):
                capacity_records[record.credential_account_id] = record
        except Exception as exc:
            print(f"capacity state: unreadable ({type(exc).__name__})")
    ok_any = False
    for account in accounts:
        record = capacity_records.get(account.account_id)
        if record is not None and record.is_limit_reached():
            print(
                f"  account {account.account_id}: capacity LIMIT-REACHED "
                f"({record.error_code or 'provider limit'}) — no NEW orders are "
                "published through it"
            )
        try:
            capability = await cloud_router.probe_account(account.account_id)
        except Exception as exc:
            print(f"  account {account.account_id}: probe failed ({type(exc).__name__})")
            continue
        if capability.accessible:
            ok_any = True
            print(f"  account {account.account_id}: Public Cloud ACCESSIBLE")
            print(f"    regions: {len(capability.regions)}")
            for region_id, type_count in sorted(capability.regions):
                # Image read status is per-region, from the OWNING account.
                try:
                    provider = cloud_router.client_for(account.account_id)
                    images = await provider.list_images(region_id)
                    print(
                        f"    region {region_id}: {type_count} instance type(s), "
                        f"{len(images)} image(s) readable"
                    )
                except Exception as exc:
                    print(
                        f"    region {region_id}: {type_count} instance type(s), "
                        f"images unreadable ({type(exc).__name__})"
                    )
        else:
            reason = _cloud_unavailable_reason(capability)
            print(f"  account {account.account_id}: Public Cloud UNAVAILABLE ({reason})")
            if capability.regions:
                for region_id, type_count in sorted(capability.regions):
                    print(f"    region {region_id}: {type_count} instance type(s)")
    try:
        await cloud_router.aclose()
    except Exception:
        pass

    # --- Stored hourly catalog (gates, never secrets) --------------------
    try:
        from cloud_platform.db.session import SessionFactory
        from cloud_platform.modules.offers.repository import (
            SqlAlchemyCatalogSyncStateRepository,
            SqlAlchemySellableOfferRepository,
        )

        rows = [
            row
            for row in await SqlAlchemySellableOfferRepository(SessionFactory).list_all()
            if row.provider_key == "leaseweb" and row.billing_model == "hourly"
        ]
        states = {
            state.provider_key: state
            for state in await SqlAlchemyCatalogSyncStateRepository(SessionFactory).list_all()
        }
    except Exception as exc:
        print(f"catalog sync status: unreadable ({type(exc).__name__})")
        return 0 if ok_any else 1
    total = len(rows)
    available = sum(1 for r in rows if r.provider_available)
    priced = sum(1 for r in rows if r.selling_price_minor > 0)
    auto_priced = sum(1 for r in rows if r.auto_priced)
    manual = sum(1 for r in rows if not r.auto_priced)
    enabled = sum(1 for r in rows if r.enabled)
    operator_disabled = sum(1 for r in rows if r.operator_disabled)
    sellable = sum(
        1
        for r in rows
        if is_sellable_in_currency(r, settings.fx_catalog_pricing_currency.strip().upper())
    )
    print(f"hourly offers stored: {total}")
    print(f"  provider_available: {available}")
    print(f"  priced: {priced}")
    print(f"  auto-priced: {auto_priced} / manual: {manual}")
    print(f"  enabled: {enabled}")
    print(f"  operator_disabled: {operator_disabled}")
    print(f"  sellable: {sellable}")
    if not sellable and total:
        from cloud_platform.modules.offers.domain import visibility_summary

        print(f"  gates: {visibility_summary(rows)}")

    policies = pricing_policies_from_settings(settings)
    policy = policies.get("leaseweb.hourly") or policies.get("leaseweb")
    if policy is not None:
        source = "leaseweb.hourly" if "leaseweb.hourly" in policies else "leaseweb"
        print(
            f"pricing-policy source: [{source}] "
            f"mode={policy.mode} markup={policy.markup_percent}% "
            f"auto_publish={'yes' if policy.auto_publish else 'no'}"
        )
    else:
        print("pricing-policy source: MISSING (costs refresh only)")
        print("action: add this non-secret block to the server-owned configuration.toml:")
        print('  [storefront.pricing."leaseweb.hourly"]')
        print('  mode = "markup"')
        print("  markup_percent = 25")
        print("  auto_publish = true")
    # Monthly/hourly sync states are separate rows ("leaseweb" vs
    # "leaseweb.hourly"); older deployments only have "leaseweb".
    state = states.get("leaseweb.hourly") or states.get("leaseweb")
    if state is not None and state.last_attempted_at is not None:
        print(f"catalog sync status: last attempt {state.last_attempted_at.isoformat()}")
        print(
            f"  last successful sync: "
            f"{state.last_success_at.isoformat() if state.last_success_at else 'never'}"
        )
        print(
            f"  last sync result: discovered={state.discovered} "
            f"persisted={state.persisted} prices={state.prices_updated} "
            f"published={state.published} retired={state.retired}"
        )
        for warning in state.warnings[:5]:
            print(f"    warning: {warning}")
        for error in state.errors[:5]:
            print(f"    error: {error}")
    else:
        print("catalog sync status: never (no coordinator run recorded for leaseweb.hourly)")
        print("action: ensure [storefront.catalog_sync] enabled = true, then check")
        print("  python -m cloud_platform.cli catalog auto-sync doctor")
    if not ok_any and not sellable:
        print("result: hourly Cloud is NOT buyable (no account serves it or nothing is sellable)")
        return 1
    if not sellable:
        print("result: provider serves Cloud but nothing is sellable yet")
        return 1
    print("result: hourly Cloud buyable (Cloud family appears in Telegram)")
    return 0


def _cloud_capacity_repository() -> Any | None:
    """Open the durable per-account capacity store (None when unavailable).

    Diagnostics must never fail because the DB layer is unreachable: capacity
    is reported as UNKNOWN in that case instead of crashing the command.
    """
    try:
        from cloud_platform.db.session import SessionFactory
        from cloud_platform.modules.provider_capacity.repository import (
            SqlAlchemyAccountCapacityRepository,
        )

        return SqlAlchemyAccountCapacityRepository(SessionFactory)
    except Exception:
        return None


async def leaseweb_cloud_accounts(action: str = "doctor", account: str | None = None) -> int:
    """Read-only per-account hourly-Cloud capacity / eligibility diagnostics.

    Answers, per credential account and without ever printing a secret:

    - the stable account id, configured state (ACTIVE/DRAINING/DISABLED) and
      selection priority;
    - this process's authentication verdict;
    - how many Cloud regions the account demonstrably serves, and how many
      instances it currently holds there;
    - its NEW-ORDER capacity state: ``healthy`` / ``limit-reached``, the
      provider error code of the last refusal (e.g. ``PC-2031``), its
      correlation id, and when the signal stops applying.

    ``clear --account <id>`` marks one account eligible for new orders again
    (the recorded refusal evidence is kept for diagnostics) — this is how an
    operator re-probes after freeing provider capacity. Nothing here places,
    retries or cancels a provider order, and no existing resource is touched.
    """
    from cloud_platform.providers.leaseweb.cloud_accounts import (
        build_cloud_account_router,
    )

    settings = get_settings()
    try:
        router = build_cloud_account_router(settings)
    except Exception as exc:
        print(f"error: cloud account router unavailable ({type(exc).__name__})")
        return 1
    if router is None:
        print("error: leaseweb hourly cloud has no credential configured")
        return 1

    accounts = list(router.accounts)
    capacity_repo = _cloud_capacity_repository()
    if capacity_repo is None:
        print("[WARN] capacity store unavailable; capacity reported as UNKNOWN")

    if action == "clear":
        target = str(account or "").strip()
        if not target:
            print("error: clear requires --account <credential account id>")
            print(f"configured accounts: {', '.join(a.account_id for a in accounts) or '-'}")
            return 2
        if target not in {a.account_id for a in accounts}:
            print(f"error: account {target!r} is not configured")
            return 2
        if capacity_repo is None:
            print("error: capacity store unavailable; nothing was cleared")
            return 1
        try:
            record = await capacity_repo.record_healthy("leaseweb", target)
        except Exception as exc:
            print(f"error: could not clear capacity state ({type(exc).__name__})")
            return 1
        print(
            f"account {target}: capacity state {record.state.value} "
            f"(previous refusals recorded: {record.observations}); "
            "it is eligible for NEW orders again"
        )
        return 0

    records: dict[str, Any] = {}
    if capacity_repo is not None:
        try:
            for record in await capacity_repo.list_for_provider("leaseweb"):
                records[record.credential_account_id] = record
        except Exception as exc:
            print(f"[WARN] capacity state unreadable ({type(exc).__name__})")

    print(f"leaseweb cloud accounts ({len(accounts)} configured)")
    limit_reached: list[str] = []
    for definition in accounts:
        account_id = definition.account_id
        print()
        print(
            f"account {account_id}: state={definition.state.value} "
            f"priority={definition.priority} enabled={'yes' if definition.enabled else 'no'}"
        )

        region_count = None
        instances = 0
        auth = "not probed (disabled)"
        if definition.enabled:
            try:
                capability = await router.probe_account(account_id)
            except Exception as exc:
                print(f"  cloud regions: unreadable ({type(exc).__name__})")
                capability = None
            if capability is None:
                auth = "unknown"
            elif capability.error_class == "AuthenticationError":
                auth = "REJECTED (AuthenticationError)"
            elif capability.accessible:
                auth = "ok"
            elif capability.error_class:
                auth = f"inconclusive ({capability.error_class})"
            else:
                # Authenticated, but this Sales Organization serves no Cloud.
                auth = "ok (no Cloud entitlement)"
            if capability is not None and capability.accessible:
                region_count = len(capability.regions)
                provider = router.client_for(account_id)
                for region_id, _types in sorted(capability.regions):
                    try:
                        instances += len(await provider.list_instances(region_id))
                    except Exception:
                        # A region this credential cannot census is not an
                        # error: it is simply not counted.
                        continue
        print(f"  authentication: {auth}")
        print(
            "  cloud regions proven accessible: "
            + (str(region_count) if region_count is not None else "unknown")
        )
        print(f"  instances currently held: {instances}")

        record = records.get(account_id)
        if record is None:
            print("  capacity: healthy (never observed)")
            continue
        if record.is_limit_reached():
            reference = record.observed_at.isoformat() if record.observed_at else "-"
            expires = record.expires_at.isoformat() if record.expires_at else "-"
            print(
                f"  capacity: LIMIT-REACHED ({record.error_code or 'provider limit'}) "
                "- no NEW orders are published through this account"
            )
            print(f"    last refusal: {reference}; applies until {expires}")
            print(f"    correlationId: {record.correlation_id or '-'}")
            print(f"    affected: {record.location_id or '-'} / {record.product_id or '-'}")
            limit_reached.append(account_id)
        else:
            print(
                f"  capacity: healthy (refusals recorded: {record.observations}"
                + ("; signal expired" if record.state.value == "limit_reached" else "")
                + ")"
            )

    print()
    if limit_reached:
        print("result: NEW hourly orders are NOT published through: " + ", ".join(limit_reached))
        print(
            "action: free capacity on the provider account (or wait for the "
            "configured TTL), then run 'leaseweb cloud accounts clear --account <id>'"
        )
        return 1
    print("result: every configured account may receive NEW hourly orders")
    return 0


async def leaseweb_cloud_catalog(region: str | None) -> int:
    """Read-only: normalized hourly catalog (regions, types, prices, images)."""
    provider = await _hourly_cloud_provider_or_error()
    if provider is None:
        return 1
    try:
        regions = await provider.list_regions()
    except Exception as exc:
        print(f"error: regions unreadable ({type(exc).__name__})")
        return 1
    wanted = [r for r in regions if region is None or r.id == region]
    if region is not None and not wanted:
        print(f"error: unknown region {region!r}")
        return 1
    from cloud_platform.modules.fx.formatting import format_minor

    try:
        from cloud_platform.db.session import SessionFactory
        from cloud_platform.modules.offers.repository import (
            SqlAlchemySellableOfferRepository,
        )

        stored_offers = {
            (row.product_id, row.location_id): row
            for row in await SqlAlchemySellableOfferRepository(SessionFactory).list_all()
            if row.provider_key == "leaseweb"
        }
    except Exception:
        stored_offers = {}
    for item in sorted(wanted, key=lambda r: r.id):
        print(f"== {item.id} ({item.country_code or '??'}) — {item.name}")
        try:
            types = await provider.list_instance_types(item.id)
        except Exception as exc:
            print(f"   types unreadable ({type(exc).__name__})")
            continue
        for entry in sorted(types, key=lambda e: (e.hourly_cost_minor, e.name)):
            # Never render minor units as whole currency: the provider rate
            # keeps its exact decimal text, integer fields go through the
            # shared money formatter.
            if entry.hourly_rate_exact:
                provider_line = f"provider: {entry.hourly_rate_exact} {entry.currency}/hour"
            else:
                provider_line = (
                    f"provider: {format_minor(entry.hourly_cost_minor, entry.currency)}/hour"
                )
            normalized = (
                "normalized provider cost: "
                f"{format_minor(entry.hourly_cost_minor, entry.currency)}/hour"
            )
            stored = stored_offers.get((entry.id, item.id))
            if stored is not None and stored.selling_price_minor > 0:
                selling = (
                    "selling: "
                    f"{format_minor(stored.selling_price_minor, stored.selling_currency)}/hour"
                )
            else:
                selling = "selling: — (unpriced/unpublished)"
            print(
                f"   {entry.id} [{entry.family_name}]: "
                f"{entry.vcpu} vCPU / {entry.ram_gb} GB RAM / {entry.disk_gb} GB disk"
            )
            print(f"      {provider_line} / {normalized} / {selling}")
        try:
            images = await provider.list_images(item.id)
        except Exception as exc:
            print(f"   images unreadable ({type(exc).__name__})")
            continue
        for image in sorted(images, key=lambda i: i.label):
            print(f"   image {image.id}: {image.label}")
    try:
        await provider.close()
    except Exception:
        pass
    return 0


async def leaseweb_cloud_create_preview(
    region: str,
    instance_type: str,
    image_id: str,
    reference: str,
    root_disk_size: int | None = None,
    root_disk_storage_type: str | None = None,
) -> int:
    """Print the exact hourly POST body WITHOUT sending it (never mutates).

    The launch root disk is a REQUIRED provider field; when it is not passed
    explicitly it is derived read-only from live provider facts (the type's
    ``minDiskSize`` plus the image's own ``minDiskSize`` and storage types),
    which is exactly what checkout pins into the contract.
    """
    import json

    from cloud_platform.providers.leaseweb.cloud import (
        build_create_body,
        resolve_root_disk,
    )

    provider = await _hourly_cloud_provider_or_error()
    if provider is None:
        return 1
    try:
        try:
            types = await provider.list_instance_types(region)
            images = await provider.list_images(region)
        except Exception as exc:
            print(f"error: provider catalog unreadable ({type(exc).__name__})")
            return 1
        match = next((item for item in types if item.id == instance_type), None)
        if match is None:
            print(f"error: instance type {instance_type!r} is not offered in {region!r}")
            return 1
        image = next((item for item in images if item.id == image_id), None)
        if image is None:
            print(f"error: image {image_id!r} is not offered in {region!r}")
            return 1
        effective = resolve_root_disk(
            disk_gb=match.disk_gb,
            storage_type=match.storage_type,
            image=image,
            type_storage_types=match.storage_types,
        )
        body = build_create_body(
            instance_type=instance_type,
            image_id=image_id,
            region=region,
            reference=reference,
            root_disk_size_gb=root_disk_size or effective.size_gb,
            root_disk_storage_type=root_disk_storage_type or effective.storage_type,
            image_label=image.label,
            os_family=image.os_family,
        )
    finally:
        try:
            await provider.close()
        except Exception:
            pass
    print("POST /publicCloud/v1/instances (NOT SENT — preview only):")
    print(json.dumps(body, indent=2, sort_keys=True))
    print("no provider mutation occurred")
    return 0


async def leaseweb_cloud_create(
    user_id: str, offer_id: str, image_id: str, execute_live: bool
) -> int:
    """Create an hourly instance intent (worker POSTs under the ledger).

    Refuses without ``--execute-live``: the intent leads to a BILLABLE
    provider resource once the worker submits it. Never run casually.
    """
    if not execute_live:
        print(
            "refused: creating an hourly instance leads to a BILLABLE provider "
            "resource. Re-run with --execute-live to proceed deliberately."
        )
        return 2
    from uuid import UUID

    from cloud_platform.core.container import create_container

    print(
        "WARNING: this creates a BILLABLE hourly cloud instance "
        "(the worker submits the provider POST; delete the resource to stop billing)."
    )
    container = create_container()
    try:
        await container.initialize()
        user = await container.user_repository().get(UUID(user_id))
        if user is None:
            print(f"error: unknown user {user_id}")
            return 1
        offers = container.sellable_offer_repository()
        offer = await offers.get(UUID(offer_id))
        if offer is None:
            print(f"error: unknown offer {offer_id}")
            return 1
        service = container.hourly_cloud_service()
        image = await service.cloud_image_by_id(offer, image_id)
        result = await service.create_instance(
            user=user,
            offer_id=offer.id,
            image_id=image.id,
            image_label=image.label,
            idempotency_key=f"cli-hourly:{offer.id}:{image.id}:{user.id}",
        )
    finally:
        await container.close()
    print(f"hourly create intent {result.server.id} (replayed={result.replayed})")
    print("the worker will submit POST /publicCloud/v1/instances under the operation ledger")
    return 0


async def offers_price_book(
    provider: str,
    markup_percent: int,
    dry_run: bool,
    include_disabled: bool,
) -> int:
    """Deliberately price unpriced offers from native cost + explicit markup."""
    if markup_percent < 0:
        print("markup-percent must not be negative")
        return 2
    from cloud_platform.core.container import Container, create_container
    from cloud_platform.db.session import SessionFactory
    from cloud_platform.modules.offers.domain import PricingPolicy
    from cloud_platform.modules.offers.pricing import CatalogOfferPricer, resolve_catalog_rate
    from cloud_platform.modules.offers.repository import SqlAlchemySellableOfferRepository

    settings = get_settings()
    target = str(settings.fx_catalog_pricing_currency).strip().upper()
    if target != str(settings.fx_catalog_pricing_currency).strip().upper():
        print("configured catalog currency is invalid")
        return 2
    catalog_stale_limit = int(getattr(settings, "fx_frankfurter_catalog_max_stale_seconds", 86400))
    catalog_quote_ttl = int(getattr(settings, "fx_frankfurter_quote_ttl_seconds", 3600))
    # Manual pricing must obey the SAME publication horizon as the periodic
    # catalog sync: a price that expires before the next refresh would empty the
    # storefront even though the sync itself is healthy.
    catalog_horizon = int(settings.catalog_fx_min_remaining_lifetime_seconds)
    catalog_horizon_anchor = datetime.now(UTC)
    container = create_container()
    resolver = container.global_fx_resolver_or_none()
    repo = SqlAlchemySellableOfferRepository(
        SessionFactory,
        catalog_currency=target,
        catalog_stale_limit_seconds=catalog_stale_limit,
    )
    priced = skipped = disabled_skipped = 0
    try:
        rows = [row for row in await repo.list_all() if row.provider_key == provider]
        if not rows:
            print(f"no stored offers for provider {provider!r} — sync the catalog first")
            return 1
        policy = PricingPolicy(mode="markup", markup_percent=markup_percent, auto_publish=False)
        prefetched_rates: dict[tuple[str, str], Any] = {}
        if resolver is not None:
            for row in rows:
                if row.selling_price_minor > 0 or (not row.enabled and not include_disabled):
                    continue
                source = (row.provider_cost_currency or "").strip().upper()
                destination = source if source in {"IRT", "IRR"} else target
                if source in {"", "IRT", "IRR"} or source == destination:
                    continue
                pair = (source, destination)
                if pair in prefetched_rates:
                    continue
                try:
                    prefetched_rates[pair] = await resolve_catalog_rate(
                        resolver,
                        source,
                        destination,
                        min_remaining_lifetime_seconds=catalog_horizon,
                    )
                except Exception as exc:
                    print(
                        f"  WARN {source}->{destination}: reference rate unavailable "
                        f"({type(exc).__name__})"
                    )
                    continue
        for row in sorted(rows, key=lambda o: (o.location_id, o.product_id)):
            native_currency = (row.provider_cost_currency or "").strip().upper()
            row_target = native_currency if native_currency in {"IRT", "IRR"} else target
            pricer = CatalogOfferPricer(
                resolver if row_target != native_currency else None,
                row_target,
                identity_ttl_seconds=catalog_quote_ttl,
                prefetched_rates=prefetched_rates,
                min_catalog_fresh_lifetime_seconds=catalog_horizon,
                catalog_horizon_anchor=catalog_horizon_anchor,
            )
            if row.selling_price_minor > 0:
                continue
            if not row.enabled and not include_disabled:
                disabled_skipped += 1
                skipped += 1
                continue
            try:
                priced_result = await pricer.price_auto(row, policy)
                amount = priced_result.selling_price_minor
                result_currency = priced_result.selling_currency
                metadata = dict(priced_result.pricing_metadata)
                metadata.update(
                    {
                        "provider_cost_minor": row.provider_cost_minor,
                        "provider_cost_currency": row.provider_cost_currency,
                    }
                )
            except Exception as exc:
                print(f"  warning: {row.ref}: pricing failed ({type(exc).__name__}: {exc})")
                skipped += 1
                continue
            if dry_run:
                print(f"  would price {row.ref}: {amount} {result_currency} (+{markup_percent}%)")
            else:
                rate_key = (
                    "provider_hourly_rate"
                    if row.billing_model == "hourly"
                    else "provider_monthly_rate"
                )
                expected_rate = (row.billing_parameters or {}).get(rate_key)
                result = await repo.set_auto_price_if_current(
                    row.id,
                    expected_cost_minor=row.provider_cost_minor,
                    expected_cost_currency=row.provider_cost_currency,
                    selling_price_minor=amount,
                    selling_currency=result_currency,
                    pricing_metadata=metadata,
                    expected_provider_rate=(
                        str(expected_rate) if expected_rate is not None else None
                    ),
                )
                if result is None:
                    print(f"  warning: {row.ref}: provider observation changed; retry")
                    skipped += 1
                    continue
                print(f"priced {result.ref}: {amount} {result_currency}")
            priced += 1
        if disabled_skipped and not include_disabled:
            print(f"disabled (use --include-disabled): {disabled_skipped}")
        print(f"{'would price' if dry_run else 'priced'} {priced} offer(s); skipped: {skipped}")
        return 0
    finally:
        await Container.aclose_fx(resolver)
        await container.close()


async def offers_set(offer_id: str, action: str, price: str | None, currency: str | None) -> int:
    """Operator offer commands through the audited application service."""
    from cloud_platform.core.container import create_container
    from cloud_platform.modules.offers.domain import required_selling_currency
    from cloud_platform.modules.users.domain import Role, User, UserStatus

    container = create_container()
    try:
        admin = User(
            username="cli-operator",
            email="operator@local",
            status=UserStatus.ACTIVE,
            role=Role.ADMIN,
        )
        service = container.offer_admin_service()
        repo = container.sellable_offer_repository()
        current = await repo.get(UUID(offer_id))
        if current is None:
            print(f"error: unknown offer {offer_id}")
            return 1
        if action == "enable":
            offer = await service.set_enabled(
                actor=admin,
                offer_id=current.id,
                enabled=True,
                reason="operator CLI enable",
            )
            print(f"enabled {offer.ref}")
        elif action == "disable":
            offer = await service.set_enabled(
                actor=admin,
                offer_id=current.id,
                enabled=False,
                reason="operator CLI disable",
            )
            print(f"disabled {offer.ref} (operator block recorded)")
        elif action == "price":
            if price is None:
                print("price requires a minor-unit amount")
                return 2
            selected_currency = (
                currency.strip().upper()
                if currency
                else required_selling_currency(current, get_settings().fx_catalog_pricing_currency)
            )
            offer = await service.set_selling_price(
                actor=admin,
                offer_id=current.id,
                selling_price_minor=int(price),
                currency=selected_currency,
                reason="operator CLI price",
            )
            print(f"priced {offer.ref}: {offer.selling_price_minor} {offer.selling_currency}")
        else:  # pragma: no cover
            print(f"unknown action {action}")
            return 2
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}")
        return 1
    finally:
        await container.close()
    return 0


async def offers_normalize_selling_currency(dry_run: bool, target: str = "USD") -> int:
    """Normalize catalog selling prices to the configured target currency.

    Auto-priced rows are recomputed from immutable provider cost + policy
    markup. Manual rows convert their existing selling amount with no second
    markup. FX is shared across the whole command and closed in one place.

    Idempotent and safe to re-run, including as a release transition (see
    ``scripts/deploy-production.sh``). Results are classified so the exit code
    reflects only REAL failures:

    - ``normalized`` — rows made canonical this run (or, in dry-run, that would be);
    - ``intentionally skipped`` — operator-disabled rows, rows whose provider
      has no automatic pricing policy, and manual domestic (IRT/IRR) prices.
      These are operator/user intent and never fail the command;
    - ``failed`` — pricing/FX/conversion errors and rows whose price changed
      under us (a re-run resolves those). Exit 1, because the row was left
      fail-closed and the catalog may still be incomplete.
    """
    from cloud_platform.core.container import create_container
    from cloud_platform.db.session import SessionFactory
    from cloud_platform.modules.offers.auto_sync import pricing_policies_from_settings
    from cloud_platform.modules.offers.domain import (
        has_valid_pricing_provenance,
        requires_currency_normalization,
    )
    from cloud_platform.modules.offers.pricing import CatalogOfferPricer, resolve_catalog_rate
    from cloud_platform.modules.offers.repository import SqlAlchemySellableOfferRepository

    settings = get_settings()
    target = str(target or settings.fx_catalog_pricing_currency).strip().upper()
    configured_target = str(settings.fx_catalog_pricing_currency).strip().upper()
    catalog_stale_limit = int(getattr(settings, "fx_frankfurter_catalog_max_stale_seconds", 86400))
    catalog_quote_ttl = int(getattr(settings, "fx_frankfurter_quote_ttl_seconds", 3600))
    # Normalizing storefront prices is catalog publication too: it must obey the
    # same horizon as the periodic sync or the storefront would empty again.
    catalog_horizon = int(settings.catalog_fx_min_remaining_lifetime_seconds)
    catalog_horizon_anchor = datetime.now(UTC)
    if target != configured_target:
        print(
            f"refused: --target {target} differs from configured catalog currency "
            f"{configured_target}; change configuration deliberately"
        )
        return 2
    policies = pricing_policies_from_settings(settings)
    repo = SqlAlchemySellableOfferRepository(
        SessionFactory,
        catalog_currency=settings.fx_catalog_pricing_currency,
        catalog_stale_limit_seconds=catalog_stale_limit,
    )
    container = create_container()
    resolver = container.global_fx_resolver_or_none()
    try:
        rows = [
            row
            for row in await repo.list_all()
            if requires_currency_normalization(row, target)
            or not has_valid_pricing_provenance(
                row,
                target,
                catalog_stale_limit_seconds=catalog_stale_limit,
            )
        ]
        if not rows:
            print(f"all storefront selling prices already use {target}")
            return 0
        prefetched_rates: dict[tuple[str, str], Any] = {}
        if resolver is not None:
            for row in rows:
                if row.auto_priced:
                    source_currency = (row.provider_cost_currency or "").strip().upper()
                else:
                    source_currency = (row.selling_currency or "").strip().upper()
                destination = target
                if source_currency in {"", "IRT", "IRR", destination}:
                    continue
                pair = (source_currency, destination)
                if pair in prefetched_rates:
                    continue
                try:
                    prefetched_rates[pair] = await resolve_catalog_rate(
                        resolver,
                        source_currency,
                        destination,
                        min_remaining_lifetime_seconds=catalog_horizon,
                    )
                except Exception as exc:
                    print(
                        f"  WARN {source_currency}->{destination}: reference rate "
                        f"unavailable ({type(exc).__name__})"
                    )
                    continue
        normalized = 0
        intentionally_skipped = 0
        failed = 0
        for row in sorted(rows, key=lambda o: (o.provider_key, o.location_id, o.product_id)):
            priced_minor = 0
            priced_currency = target
            if row.operator_disabled:
                print(f"  SKIP {row.ref}: operator-disabled; left untouched")
                intentionally_skipped += 1
                continue
            native = (row.provider_cost_currency or "").strip().upper()
            if row.auto_priced:
                policy = policies.get(f"{row.provider_key}.{row.billing_model}") or policies.get(
                    row.provider_key
                )
                if policy is None:
                    print(f"  SKIP {row.ref}: no automatic pricing policy")
                    intentionally_skipped += 1
                    continue
                try:
                    native_currency = (row.provider_cost_currency or "").strip().upper()
                    row_target = native_currency if native_currency in {"IRT", "IRR"} else target
                    pricer = CatalogOfferPricer(
                        resolver if row_target != native_currency else None,
                        row_target,
                        identity_ttl_seconds=catalog_quote_ttl,
                        prefetched_rates=prefetched_rates,
                        min_catalog_fresh_lifetime_seconds=catalog_horizon,
                        catalog_horizon_anchor=catalog_horizon_anchor,
                    )
                    priced = await pricer.price_auto(row, policy)
                    priced_minor = priced.selling_price_minor
                    priced_currency = priced.selling_currency
                    priced.pricing_metadata.update(
                        {
                            "provider_cost_minor": row.provider_cost_minor,
                            "provider_cost_currency": row.provider_cost_currency,
                        }
                    )
                    rate_key = (
                        "provider_hourly_rate"
                        if row.billing_model == "hourly"
                        else "provider_monthly_rate"
                    )
                    expected_rate = (row.billing_parameters or {}).get(rate_key)
                    execute = (
                        repo.set_auto_price_if_current(
                            row.id,
                            expected_cost_minor=row.provider_cost_minor,
                            expected_cost_currency=row.provider_cost_currency,
                            selling_price_minor=priced.selling_price_minor,
                            selling_currency=priced.selling_currency,
                            pricing_metadata=priced.pricing_metadata,
                            expected_provider_rate=(
                                str(expected_rate) if expected_rate is not None else None
                            ),
                        )
                        if not dry_run
                        else None
                    )
                except Exception as exc:
                    print(
                        f"  FAIL {row.ref}: pricing failed ({type(exc).__name__}); left fail-closed"
                    )
                    failed += 1
                    continue
            elif native in {"IRT", "IRR"}:
                print(
                    f"  SKIP {row.ref}: manual domestic price must remain in {native}; "
                    "operator repair required"
                )
                intentionally_skipped += 1
                continue
            else:
                try:
                    native_currency = (row.provider_cost_currency or "").strip().upper()
                    manual_currency = (row.selling_currency or "").strip().upper()
                    if native_currency in {"IRT", "IRR"} or manual_currency in {"IRT", "IRR"}:
                        raise ValueError("manual domestic pricing requires operator repair")
                    pricer = CatalogOfferPricer(
                        resolver if manual_currency != target else None,
                        target,
                        identity_ttl_seconds=catalog_quote_ttl,
                        prefetched_rates=prefetched_rates,
                        min_catalog_fresh_lifetime_seconds=catalog_horizon,
                        catalog_horizon_anchor=catalog_horizon_anchor,
                    )
                    priced = await pricer.price_manual(row)
                    priced_minor = priced.selling_price_minor
                    priced_currency = priced.selling_currency
                    execute = (
                        repo.set_manual_price(
                            row.id,
                            priced.selling_price_minor,
                            priced.selling_currency,
                            priced.pricing_metadata,
                            expected_cost_minor=row.provider_cost_minor,
                            expected_cost_currency=row.provider_cost_currency,
                            expected_updated_at=row.updated_at,
                        )
                        if not dry_run
                        else None
                    )
                except Exception as exc:
                    print(
                        f"  FAIL {row.ref}: selling-price conversion failed "
                        f"({type(exc).__name__}); left fail-closed"
                    )
                    failed += 1
                    continue
            if dry_run:
                metadata = dict(priced.pricing_metadata)
                fx_source = metadata.get("fx_source", metadata.get("fx_provider", ""))
                fx_path = metadata.get("fx_source_market", metadata.get("fx_path", ""))
                fx_rate = metadata.get("fx_rate", metadata.get("rate", ""))
                fx_observed = metadata.get("fx_observed_at", metadata.get("observed_at", ""))
                fx_expires = metadata.get("fx_expires_at", metadata.get("expires_at", ""))
                fx_date = metadata.get("fx_provider_date", "n/a")
                fx_stale = metadata.get("fx_stale", False)
                valid_until = metadata.get("catalog_valid_until", "n/a")
                print(
                    f"  WOULD {row.ref}: {row.selling_price_minor} {row.selling_currency}"
                    f" -> {priced_minor} {priced_currency}"
                )
                print(
                    "    FX provenance: "
                    f"source={fx_source or 'n/a'} path={fx_path or 'n/a'} "
                    f"rate={fx_rate or 'n/a'} provider_date={fx_date} "
                    f"observed_at={fx_observed or 'n/a'} expires_at={fx_expires or 'n/a'} "
                    f"stale={fx_stale} valid_until={valid_until}"
                )
            else:
                try:
                    result = await execute  # type: ignore[misc]
                except Exception as exc:
                    # The price book re-validates the converted price against
                    # the row's OWN provider facts (exact provider rate +
                    # provider minor cost) and refuses a price it cannot prove.
                    # That is a row-level failure, never a whole-run abort: the
                    # row stays fail-closed, the counters show it, and the
                    # usual cause is a stale/missing provider observation that
                    # the catalog refresh repairs.
                    print(
                        f"  FAIL {row.ref}: the price book rejected the converted "
                        f"price ({type(exc).__name__}); left fail-closed. A stale or "
                        "missing provider cost/rate fact is the usual cause; run "
                        "'catalog auto-sync run' to refresh provider facts, then re-run"
                    )
                    failed += 1
                    continue
                if result is None:
                    print(
                        f"  FAIL {row.ref}: concurrent pricing/manual change won; "
                        "re-run to normalize it"
                    )
                    failed += 1
                    continue
                print(f"  OK   {row.ref}: {priced_minor} {priced_currency}")
            normalized += 1
        verb = "would normalize" if dry_run else "normalized"
        print(
            f"{verb} {normalized} offer(s) to {target}; "
            f"intentionally skipped: {intentionally_skipped}; failed: {failed}"
        )
        # An operator-disabled plan is INTENT, not a failure: only rows left
        # fail-closed (unpriced or unpriced-able) fail the command.
        return 0 if failed == 0 else 1
    finally:
        await container.close()
        from cloud_platform.core.container import Container

        await Container.aclose_fx(resolver)


async def offers_readiness() -> int:
    """Read-only release gate: is the customer-facing catalog actually open?

    Machine-usable (exit 0 = ready, 1 = not) and provider-neutral. A release
    must never end with a configured, enabled, credentialed and auto-priced
    provider having NOTHING on sale: that is exactly how the global-USD
    release shipped a green deployment with an empty storefront (507 stored
    rows, every one hidden by the canonical-currency/provenance gates).

    Only providers the operator actually serves are considered, so a provider
    without a market, disabled with ``enabled = false``, without a credential,
    or without an automatic pricing policy is never required to have offers.
    Operator intent is preserved as well: when EVERY stored offer of such a
    provider is operator-disabled, the closed store is reported, not failed.
    Read-only; never prints a credential.
    """
    from cloud_platform.core.container import create_container
    from cloud_platform.db.session import SessionFactory
    from cloud_platform.modules.offers.auto_sync import pricing_policies_from_settings
    from cloud_platform.modules.offers.domain import is_sellable_in_currency
    from cloud_platform.modules.offers.repository import (
        SqlAlchemyCatalogSyncStateRepository,
        SqlAlchemySellableOfferRepository,
    )

    settings = get_settings()
    catalog_currency = str(settings.fx_catalog_pricing_currency).strip().upper()
    policies = pricing_policies_from_settings(settings)
    try:
        rows = await SqlAlchemySellableOfferRepository(SessionFactory).list_all()
    except Exception as exc:
        print(f"[FAIL] the offer price book could not be read ({type(exc).__name__})")
        print("storefront readiness: FAIL")
        return 1
    states: dict[str, Any] = {}
    try:
        states = {
            state.provider_key: state
            for state in await SqlAlchemyCatalogSyncStateRepository(SessionFactory).list_all()
        }
    except Exception:
        states = {}

    # Which providers the operator actually serves: the provider registry is
    # built from credentials, exactly like the API/worker/bot processes.
    catalog: Any = None
    registry_keys: set[str] = set()
    registry_available = False
    container = None
    try:
        container = create_container()
        await container.initialize()
        catalog = container.market_catalog()
        registry_keys = {str(key) for key in container.provider_registry.keys()}
        registry_available = True
    except Exception as exc:  # pragma: no cover - depends on local settings
        print(f"[WARN] provider registry unavailable ({type(exc).__name__})")
    finally:
        if container is not None:
            await container.close()

    stored: dict[str, int] = {}
    sellable: dict[str, int] = {}
    operator_disabled: dict[str, int] = {}
    for row in rows:
        key = str(row.provider_key)
        stored[key] = stored.get(key, 0) + 1
        if is_sellable_in_currency(row, catalog_currency):
            sellable[key] = sellable.get(key, 0) + 1
        if getattr(row, "operator_disabled", False):
            operator_disabled[key] = operator_disabled.get(key, 0) + 1

    # A pricing policy may be family-specific ("leaseweb.hourly"); such a
    # family belongs to its base provider.
    policy_bases: set[str] = set()
    for policy_key in policies:
        policy_bases.add(policy_key.partition(".")[0])
    # Without a registry (unbuildable container) the providers that own stored
    # rows are the only ones we can prove are served. Requiring a credential is
    # a SAFETY property here: disabled/uncredentialed providers are never
    # forced to have offers.
    served = registry_keys if registry_available else set(stored)

    lines: list[str] = []
    failures: list[str] = []
    for provider_key in sorted(set(stored) | policy_bases | served):
        # Configuration gates are only applied when the configuration could be
        # READ: an unbuildable container must never turn "I cannot see the
        # market configuration" into a passing readiness gate.
        if catalog is not None:
            market = catalog.market_of(provider_key)
            enabled = catalog.is_enabled(provider_key)
            if market is None or not enabled:
                lines.append(f"[SKIP] {provider_key}: not configured/enabled in the storefront")
                continue
        if provider_key not in served:
            lines.append(f"[SKIP] {provider_key}: no credential configured")
            continue
        if provider_key not in policy_bases:
            lines.append(
                f"[SKIP] {provider_key}: no automatic pricing policy "
                "(costs refresh only; pricing is manual)"
            )
            continue
        total = stored.get(provider_key, 0)
        if total == 0:
            # A provider that has never synced is NOT a release failure: the
            # periodic refresh (and its startup pass) is exactly what lands
            # the catalog. A recorded run that DISCOVERED products but stored
            # none is a failure.
            discovered = sum(
                int(getattr(state, "discovered", 0) or 0)
                for key, state in states.items()
                if key == provider_key or key.startswith(f"{provider_key}.")
            )
            if discovered > 0:
                failures.append(
                    f"{provider_key}: the last catalog run discovered {discovered} "
                    "plan(s) but stored no offer"
                )
            else:
                lines.append(
                    f"[WARN] {provider_key}: no stored offers yet (catalog sync has not landed)"
                )
            continue
        on_sale = sellable.get(provider_key, 0)
        if on_sale > 0:
            lines.append(f"[OK  ] {provider_key}: {on_sale} sellable of {total} stored")
            continue
        if operator_disabled.get(provider_key, 0) == total:
            lines.append(
                f"[WARN] {provider_key}: all {total} stored offer(s) are "
                "operator-disabled (store closed by operator intent)"
            )
            continue
        failures.append(
            f"{provider_key}: {total} stored offer(s) but ZERO sellable in "
            f"{catalog_currency} (none has a canonical currency plus valid FX provenance)"
        )

    for line in lines:
        print(line)
    if failures:
        for message in failures:
            print(f"[FAIL] {message}")
        print("storefront readiness: FAIL")
        print(
            "  action: python -m cloud_platform.cli offers normalize-selling-currency "
            f"--target {catalog_currency} --dry-run, then --execute, then re-run the "
            "catalog sync"
        )
        return 1
    print("storefront readiness: OK")
    return 0


async def users_find(telegram_id: int) -> int:
    from cloud_platform.db.session import SessionFactory
    from cloud_platform.modules.users.repository import SqlAlchemyUserRepository

    repo = SqlAlchemyUserRepository(SessionFactory)
    user = await repo.get_by_telegram_user_id(telegram_id)
    if user is None:
        print(f"no user with telegram id {telegram_id}")
        return 1
    print(
        f"id={user.id} username={user.username} status={user.status.value} role={user.role.value}"
    )
    return 0


async def wallet_balance(user_id: str) -> int:
    from cloud_platform.db.session import SessionFactory
    from cloud_platform.modules.wallet.repository import SqlAlchemyWalletRepository

    repo = SqlAlchemyWalletRepository(SessionFactory)
    wallet = await repo.get(UUID(user_id))
    if wallet is None:
        print(f"no wallet for user {user_id}")
        return 1
    print(f"balance={wallet.balance} {wallet.currency} status={wallet.status.value}")
    return 0


async def wallet_adjust(user_id: str, amount: int, reason: str) -> int:
    from cloud_platform.core.container import create_container
    from cloud_platform.modules.users.domain import Role, User, UserStatus

    if not reason or not reason.strip():
        print("a non-empty reason is required")
        return 2
    container = create_container()
    try:
        admin = User(
            username="cli-operator",
            email="operator@local",
            status=UserStatus.ACTIVE,
            role=Role.ADMIN,
        )
        import hashlib

        digest = hashlib.sha256(reason.encode("utf-8")).hexdigest()[:12]
        service = container.wallet_admin_service()
        wallet, entry = await service.adjust_balance(
            admin=admin,
            user_id=UUID(user_id),
            amount=amount,
            reason=reason,
            idempotency_key=f"cli-adj:{UUID(user_id)}:{amount}:{digest}",
        )
        print(
            f"adjusted {amount} {wallet.currency}: new balance {wallet.balance} (ledger {entry.id})"
        )
    finally:
        await container.close()
    return 0


async def orders_list(attention_only: bool) -> int:
    from cloud_platform.db.session import SessionFactory
    from cloud_platform.modules.orders.repository import SqlAlchemyProviderOrderRepository

    repo = SqlAlchemyProviderOrderRepository(SessionFactory)
    orders = (
        await repo.list_attention("leaseweb")
        if attention_only
        else await repo.list_open("leaseweb")
    )
    if not orders:
        print("no orders" + (" needing attention" if attention_only else " open"))
        return 0
    for order in orders:
        print(
            f"{order.id}  status={order.status.value:12s} server={order.server_id} "
            f"provider_order={order.provider_order_id or '-'} attempts={order.attempts} "
            f"error={order.error or ''}"
        )
    return 0


async def orders_inspect(order_id: str) -> int:
    from cloud_platform.db.session import SessionFactory
    from cloud_platform.modules.operations.repository import SqlAlchemyOperationRepository
    from cloud_platform.modules.orders.repository import SqlAlchemyProviderOrderRepository

    repo = SqlAlchemyProviderOrderRepository(SessionFactory)
    order = await repo.get(UUID(order_id))
    if order is None:
        print(f"order {order_id} not found")
        return 1
    print(f"id={order.id} status={order.status.value}")
    print(f"server={order.server_id} offer={order.offer_id} provider={order.provider_key}")
    print(f"provider_order={order.provider_order_id or '-'}")
    print(
        f"contract={order.provider_contract_id or '-'} service={order.provider_service_id or '-'}"
    )
    print(f"delivery_estimate={order.delivery_estimate or '-'}")
    print(f"attempts={order.attempts} error={order.error or '-'}")
    op_repo = SqlAlchemyOperationRepository(SessionFactory)
    op = await op_repo.get_by_key(order.operation_key)
    if op is not None:
        print(f"operation={op.id} status={op.status.value} attempts={op.attempts}")
    return 0


def _cli_admin_user() -> Any:
    from cloud_platform.modules.users.domain import Role, User, UserStatus

    return User(
        username="cli-operator",
        email="operator@local",
        status=UserStatus.ACTIVE,
        role=Role.ADMIN,
    )


async def orders_retry(order_id: str, reason: str) -> int:
    """Reopen a DEFINITIVELY FAILED order intent for a worker retry.

    Only definitive failures (provider rejection, nothing created) qualify.
    Ambiguous orders (OUTCOME_UNKNOWN / NEEDS_REVIEW) are REFUSED here: they
    must be resolved with ``orders resolve-existing`` / ``resolve-not-created``
    after the operator verified the provider state at the portal.
    """
    from cloud_platform.core.container import create_container
    from cloud_platform.modules.orders.service import OrderManualResolutionError

    container = create_container()
    try:
        service = container.order_manual_resolution()
        order, op = await service.retry_failed(
            UUID(order_id), actor=_cli_admin_user(), reason=reason
        )
    except OrderManualResolutionError as exc:
        print(f"REFUSED: {exc}")
        return 2
    except LookupError as exc:
        print(exc)
        return 1
    finally:
        await container.close()
    print(
        f"order {order.id} reopened (operation {op.id} status={op.status.value}, "
        f"server re-queued); the worker will re-attempt with the SAME local "
        "operation key."
    )
    print(
        "Leaseweb ordering has NO provider-side idempotency: this retry is safe ONLY "
        "because the previous attempt was DEFINITIVELY rejected without creating an "
        "order. Verify at the portal before retrying if there is any doubt."
    )
    return 0


async def orders_resolve_existing(
    order_id: str, provider_order_id: str, reason: str, yes: bool
) -> int:
    """Attach the provider order id a human VERIFIED at the Leaseweb portal
    corresponds to an ambiguous POST. Performs a READ-ONLY validation of the
    supplied id; NEVER POSTs; captures the hold exactly once."""
    if not yes:
        print(
            "REFUSED: pass --yes to confirm. This attaches a provider order id you "
            "verified at the Leaseweb portal; it NEVER POSTs to Leaseweb."
        )
        return 2
    from cloud_platform.core.container import create_container
    from cloud_platform.modules.orders.service import OrderManualResolutionError

    container = create_container()
    try:
        service = container.order_manual_resolution()
        order, op = await service.resolve_existing(
            UUID(order_id),
            provider_order_id,
            actor=_cli_admin_user(),
            reason=reason,
        )
    except OrderManualResolutionError as exc:
        print(f"REFUSED: {exc}")
        return 2
    except LookupError as exc:
        print(exc)
        return 1
    finally:
        await container.close()
    print(
        f"order {order.id} resolved: attached provider_order_id="
        f"{order.provider_order_id} status={order.status.value} "
        f"operation={op.status.value}"
    )
    if order.settlement_status.value == "complete":
        print(
            "Settlement COMPLETE: the wallet hold was captured exactly once with "
            "its CHARGE ledger entry. The read-only reconciler will poll the "
            "order; delivery is allowed. No provider POST was made."
        )
    elif order.settlement_status.value == "needs_review":
        print(
            "ATTENTION: settlement NEEDS_REVIEW — "
            f"{order.settlement_error or 'see order'}. The provider order id "
            "stays attached and the hold stays reserved; NO delivery will happen "
            "until a human resolves the financial state. No provider POST was made."
        )
    else:
        print(
            "Settlement PENDING: the provider order id stays attached and the "
            "hold stays reserved; the reconciler retries the LOCAL capture only "
            "(never a provider POST). Delivery is BLOCKED until settlement is "
            "complete."
        )
    return 0


async def orders_resolve_vps(order_id: str, vps_id: str, reason: str, yes: bool) -> int:
    """Attach the provisioned VPS id a human VERIFIED at the Leaseweb portal
    for an order whose exact resource identity never became provable
    automatically (no usable equipmentId). READ-ONLY validation of the VPS;
    NEVER POSTs; requires settlement COMPLETE; activates + delivers through
    the normal activator."""
    if not yes:
        print(
            "REFUSED: pass --yes to confirm. This attaches the exact provider "
            "VPS id you verified at the Leaseweb portal for this order; it NEVER "
            "POSTs to Leaseweb and requires the payment settlement to be complete."
        )
        return 2
    from cloud_platform.core.container import create_container
    from cloud_platform.modules.orders.service import OrderManualResolutionError

    container = create_container()
    try:
        service = container.order_manual_resolution()
        order, _op = await service.resolve_vps(
            UUID(order_id), vps_id, actor=_cli_admin_user(), reason=reason
        )
    except OrderManualResolutionError as exc:
        print(f"REFUSED: {exc}")
        return 2
    except LookupError as exc:
        print(exc)
        return 1
    finally:
        await container.close()
    print(
        f"order {order.id} resolved: attached provider VPS id {vps_id} "
        f"(provider_order_id={order.provider_order_id} status={order.status.value})"
    )
    print(
        "Settlement was complete; the server is now RUNNING with its renewal "
        "record, and the owning user was notified with the server details. "
        "No provider POST was made."
    )
    return 0


async def orders_resolve_not_created(order_id: str, reason: str, yes: bool) -> int:
    """Re-queue an ambiguous order intent AFTER the operator verified at the
    Leaseweb portal that the ambiguous POST created NO order. The retry is a
    NEW provider POST under the same local operation key — Leaseweb has no
    provider-side idempotency — so this is only safe after verified absence.
    """
    if not yes:
        print(
            "REFUSED: pass --yes to confirm. This re-queues the intent so the worker "
            "will place a NEW provider order. Only do this after you verified at the "
            "Leaseweb portal that the ambiguous POST created nothing."
        )
        return 2
    from cloud_platform.core.container import create_container
    from cloud_platform.modules.orders.service import OrderManualResolutionError

    container = create_container()
    try:
        service = container.order_manual_resolution()
        order, op = await service.resolve_not_created(
            UUID(order_id), actor=_cli_admin_user(), reason=reason
        )
    except OrderManualResolutionError as exc:
        print(f"REFUSED: {exc}")
        return 2
    except LookupError as exc:
        print(exc)
        return 1
    finally:
        await container.close()
    print(
        f"order {order.id} re-queued (operation {op.id} status={op.status.value}) "
        "with the SAME local operation key."
    )
    print(
        "WARNING: the worker will now place a NEW provider order. Leaseweb provides "
        "NO provider-side idempotency — this is safe ONLY because you verified the "
        "ambiguous POST created nothing at the portal."
    )
    return 0


async def renewals_list(attention_only: bool) -> int:
    from cloud_platform.db.session import SessionFactory
    from cloud_platform.modules.renewals.repository import SqlAlchemyRenewalRepository

    repo = SqlAlchemyRenewalRepository(SessionFactory)
    records = await repo.list_needing_attention() if attention_only else await repo.list_active()
    if not records:
        print("no renewals" + (" needing attention" if attention_only else " tracked"))
        return 0
    now = datetime.now(UTC)
    for record in sorted(records, key=lambda r: r.provider_renewal_at or datetime.min):
        days = ""
        if record.provider_renewal_at is not None:
            days = f" in {(record.provider_renewal_at - now).days}d"
        print(
            f"{record.server_id}  status={record.status.value:28s} renewal="
            f"{(record.provider_renewal_at or datetime.min).strftime('%Y-%m-%d')}{days} "
            f"price={record.customer_price_minor} {record.currency} "
            f"auto={record.auto_charge_enabled} est={record.renewal_date_estimated}"
        )
    return 0


async def renewals_check() -> int:
    from cloud_platform.core.container import create_container

    container = create_container()
    try:
        checker = container.renewal_checker()
        outcomes = await checker.run()
    finally:
        await container.close()
    for outcome in outcomes:
        print(
            f"{outcome.server_id}  {outcome.action}"
            + (f"  balance={outcome.balance_minor}" if outcome.balance_minor is not None else "")
        )
    return 0


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    from cloud_platform.modules.markets.domain import MARKET_ORDER

    parser = argparse.ArgumentParser(prog="cloud_platform.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    lsw = sub.add_parser("leaseweb", help="Leaseweb ordering operations")
    lsw_sub = lsw.add_subparsers(dest="subcommand", required=True)
    lsw_sub.add_parser("doctor", help="read-only pre-flight diagnostics")
    lsw_sub.add_parser("sync-offers", help="refresh the sellable-offer price book")
    lsw_sub.add_parser("auth-check", help="read-only API key check")
    lsw_sub.add_parser("coverage", help="print the VPS API coverage matrix")

    htz = sub.add_parser("hetzner", help="Hetzner catalog operations")
    htz_sub = htz.add_subparsers(dest="subcommand", required=True)
    htz_sub.add_parser("doctor", help="read-only pre-flight diagnostics")
    htz_sub.add_parser("sync-offers", help="refresh the sellable-offer price book")

    accounts = lsw_sub.add_parser("accounts", help="read-only credential accounts")
    accounts_sub = accounts.add_subparsers(dest="leaseweb_accounts", required=True)
    accounts_sub.add_parser("list", help="safe per-account inventory (never a key)")
    accounts_sub.add_parser("doctor", help="per-account authentication pre-flight")

    products = lsw_sub.add_parser("products", help="read-only ordering catalogue")
    products_sub = products.add_subparsers(dest="leaseweb_products", required=True)
    products_list = products_sub.add_parser("list")
    products_list.add_argument("--location", default=None)
    product_show = products_sub.add_parser("show")
    product_show.add_argument("product_id")
    product_show.add_argument("--location", required=True)
    product_show.add_argument("--os", default=None)
    product_show.add_argument("--control-panel", dest="control_panel", default=None)
    product_show.add_argument("--disk-upgrade", dest="disk_upgrade", default=None)
    product_show.add_argument("--contract-term", dest="contract_term", default=None)
    product_show.add_argument("--billing-cycle", dest="billing_cycle", default=None)
    product_show.add_argument("--sla", default=None)

    lsw_orders = lsw_sub.add_parser("orders", help="read-only account orders")
    lsw_orders_sub = lsw_orders.add_subparsers(dest="leaseweb_orders", required=True)
    lsw_orders_list = lsw_orders_sub.add_parser("list")
    lsw_orders_list.add_argument("--limit", type=int, default=20)
    lsw_order_show = lsw_orders_sub.add_parser("show")
    lsw_order_show.add_argument("order_id")

    lsw_cloud = lsw_sub.add_parser("cloud", help="hourly cloud operations")
    lsw_cloud_sub = lsw_cloud.add_subparsers(dest="leaseweb_cloud", required=True)
    lsw_cloud_sub.add_parser("doctor", help="read-only regions/types/images per region")
    lsw_cloud_accounts = lsw_cloud_sub.add_parser(
        "accounts",
        help="read-only per-account capacity state (doctor | clear)",
    )
    lsw_cloud_accounts.add_argument(
        "action",
        nargs="?",
        choices=["doctor", "clear"],
        default="doctor",
        help="doctor reports; clear marks one account eligible again (keeps evidence)",
    )
    lsw_cloud_accounts.add_argument(
        "--account",
        default=None,
        help="credential account id (required by 'clear')",
    )
    lsw_cloud_catalog = lsw_cloud_sub.add_parser(
        "catalog", help="read-only normalized hourly catalog"
    )
    lsw_cloud_catalog.add_argument("--region", default=None)
    lsw_cloud_preview = lsw_cloud_sub.add_parser(
        "create-preview", help="print the exact POST body without sending it"
    )
    lsw_cloud_preview.add_argument("--region", required=True)
    lsw_cloud_preview.add_argument("--type", required=True)
    lsw_cloud_preview.add_argument("--image", required=True)
    lsw_cloud_preview.add_argument("--reference", default="preview-only")
    lsw_cloud_preview.add_argument(
        "--root-disk-size",
        dest="root_disk_size",
        type=int,
        default=None,
        help="rootDiskSize in GB (default: derive from live provider facts)",
    )
    lsw_cloud_preview.add_argument(
        "--root-disk-storage-type",
        dest="root_disk_storage_type",
        default=None,
        help="rootDiskStorageType (default: derive from live provider facts)",
    )
    lsw_cloud_create = lsw_cloud_sub.add_parser(
        "create", help="create an hourly instance intent (BILLABLE)"
    )
    lsw_cloud_create.add_argument("--user-id", required=True)
    lsw_cloud_create.add_argument("--offer-id", required=True)
    lsw_cloud_create.add_argument("--image-id", dest="image_id", required=True)
    lsw_cloud_create.add_argument(
        "--execute-live",
        action="store_true",
        help="required deliberate flag: creates a BILLABLE resource",
    )
    lsw_vps = lsw_sub.add_parser("vps", help="read-only VPS inspection")
    lsw_vps_sub = lsw_vps.add_subparsers(dest="leaseweb_vps", required=True)
    lsw_vps_sub.add_parser("list")
    lsw_vps_show = lsw_vps_sub.add_parser("show")
    lsw_vps_show.add_argument("vps_id")
    lsw_vps_ips = lsw_vps_sub.add_parser("ips")
    lsw_vps_ips.add_argument("vps_id")
    lsw_vps_metrics = lsw_vps_sub.add_parser("metrics")
    lsw_vps_metrics.add_argument("vps_id")
    lsw_vps_metrics.add_argument("--from", dest="date_from", required=True)
    lsw_vps_metrics.add_argument("--to", dest="date_to", required=True)
    lsw_vps_metrics.add_argument("--granularity", default="DAY")
    lsw_vps_metrics.add_argument("--aggregation", default="SUM")
    lsw_vps_snapshots = lsw_vps_sub.add_parser("snapshots")
    lsw_vps_snapshots.add_argument("vps_id")
    lsw_vps_monitoring = lsw_vps_sub.add_parser("monitoring")
    lsw_vps_monitoring.add_argument("vps_id")

    offers = sub.add_parser("offers", help="sellable-offer management")
    offers_sub = offers.add_subparsers(dest="subcommand", required=True)
    offers_list_p = offers_sub.add_parser("list")
    offers_list_p.add_argument("--all", action="store_true", help="include disabled/unpriced")
    offers_sub.add_parser("doctor", help="read-only: why the catalog is empty")
    offers_sub.add_parser(
        "readiness",
        help="release gate: is anything actually on sale for an enabled provider?",
    )
    preview = offers_sub.add_parser(
        "preview", help="print the customer catalog exactly as the bot builds it"
    )
    preview.add_argument("--market", choices=[m.value for m in MARKET_ORDER], default=None)
    price_book = offers_sub.add_parser(
        "price-book", help="bulk-price UNPRICED offers from their synced provider cost"
    )
    price_book.add_argument("--provider", default="leaseweb")
    price_book.add_argument(
        "--markup-percent", type=int, required=True, help="operator-chosen markup over cost"
    )
    price_book.add_argument("--dry-run", action="store_true")
    price_book.add_argument(
        "--include-disabled",
        action="store_true",
        help="also price offers the operator has switched off",
    )
    for action in ("enable", "disable"):
        p = offers_sub.add_parser(action)
        p.add_argument("offer_id")
    price = offers_sub.add_parser("price")
    price.add_argument("offer_id")
    price.add_argument("minor", type=int, help="selling price in minor units (e.g. 1299 = 12.99)")
    price.add_argument("currency", nargs="?", default=None)
    normalize = offers_sub.add_parser(
        "normalize-selling-currency",
        help="convert foreign-currency offers to a single selling currency (USD)",
    )
    normalize.add_argument(
        "--target",
        default=None,
        help="target selling currency (defaults to [fx] catalog_pricing_currency)",
    )
    mode = normalize.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="print changes without applying")
    mode.add_argument("--execute", action="store_true", help="apply the currency normalization")

    catalog = sub.add_parser("catalog", help="automatic catalog sync operations")
    catalog_sub = catalog.add_subparsers(dest="subcommand", required=True)
    auto_sync = catalog_sub.add_parser("auto-sync", help="periodic offer refresh")
    auto_sync_sub = auto_sync.add_subparsers(dest="subsubcommand", required=True)
    auto_sync_sub.add_parser("doctor", help="read-only sync configuration and status")
    auto_sync_sub.add_parser(
        "run", help="run one complete catalog refresh now (bounded release budget)"
    )

    config = sub.add_parser("config", help="operator configuration diagnostics")
    config_sub = config.add_subparsers(dest="subcommand", required=True)
    config_sub.add_parser(
        "doctor", help="read-only: compare the real config with the canonical template"
    )

    users = sub.add_parser("users")
    users_sub = users.add_subparsers(dest="subcommand", required=True)
    find = users_sub.add_parser("find")
    find.add_argument("telegram_id", type=int)

    wallet = sub.add_parser("wallet")
    wallet_sub = wallet.add_subparsers(dest="subcommand", required=True)
    bal = wallet_sub.add_parser("balance")
    bal.add_argument("user_id")
    for action in ("credit", "debit"):
        p = wallet_sub.add_parser(action)
        p.add_argument("user_id")
        p.add_argument("minor", type=int, help="amount in minor units (positive)")
        p.add_argument("reason")

    orders = sub.add_parser("orders")
    orders_sub = orders.add_subparsers(dest="subcommand", required=True)
    orders_list_p = orders_sub.add_parser("list")
    orders_list_p.add_argument("--attention", action="store_true")
    orders_sub.add_parser("attention")
    inspect = orders_sub.add_parser("inspect")
    inspect.add_argument("order_id")
    retry = orders_sub.add_parser("retry", help="reopen a DEFINITIVELY FAILED order intent")
    retry.add_argument("order_id")
    retry.add_argument("--reason", default="operator retry of a definitively failed order")
    resolve_existing_p = orders_sub.add_parser(
        "resolve-existing", help="attach a provider order id verified at the portal"
    )
    resolve_existing_p.add_argument("order_id")
    resolve_existing_p.add_argument("provider_order_id")
    resolve_existing_p.add_argument("--reason", required=True)
    resolve_existing_p.add_argument("--yes", action="store_true")
    resolve_vps_p = orders_sub.add_parser(
        "resolve-vps", help="attach the provisioned VPS id verified at the portal"
    )
    resolve_vps_p.add_argument("order_id")
    resolve_vps_p.add_argument("vps_id")
    resolve_vps_p.add_argument("--reason", required=True)
    resolve_vps_p.add_argument("--yes", action="store_true")
    resolve_not_created_p = orders_sub.add_parser(
        "resolve-not-created", help="re-queue an intent after verified non-creation"
    )
    resolve_not_created_p.add_argument("order_id")
    resolve_not_created_p.add_argument("--reason", required=True)
    resolve_not_created_p.add_argument("--yes", action="store_true")

    renewals = sub.add_parser("renewals")
    renewals_sub = renewals.add_subparsers(dest="subcommand", required=True)
    rl = renewals_sub.add_parser("list")
    rl.add_argument("--attention", action="store_true")
    renewals_sub.add_parser("check")

    fx = sub.add_parser("fx", help="currency / FX resolution (read-only)")
    fx_sub = fx.add_subparsers(dest="subcommand", required=True)
    fx_sub.add_parser("doctor", help="read-only FX pre-flight diagnostics")
    fx_rates = fx_sub.add_parser("rates", help="show Frankfurter reference rates")
    fx_rates.add_argument(
        "--target",
        default=None,
        help="quote currency (must match configured catalog currency)",
    )

    return parser


async def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "fx":
        if args.subcommand == "doctor":
            result = await fx_doctor()
            print("\n".join(result.lines))
            return 0 if result.ok else 1
        if args.subcommand == "rates":
            return await fx_rates(args.target)
        return 2
    if args.command == "leaseweb":
        if args.subcommand == "doctor":
            result = await leaseweb_doctor()
            print("\n".join(result.lines))
            return 0 if result.ok else 1
        if args.subcommand == "sync-offers":
            return await leaseweb_sync_offers()
        if args.subcommand == "auth-check":
            return await leaseweb_auth_check()
        if args.subcommand == "coverage":
            return leaseweb_coverage()
        if args.subcommand == "accounts":
            if args.leaseweb_accounts == "list":
                return await leaseweb_accounts_list()
            result = await leaseweb_accounts_doctor()
            print("\n".join(result.lines))
            return 0 if result.ok else 1
        if args.subcommand == "products":
            if args.leaseweb_products == "list":
                return await leaseweb_products_list(args.location)
            return await leaseweb_product_show(args)
        if args.subcommand == "orders":
            if args.leaseweb_orders == "list":
                return await leaseweb_orders_list(args.limit)
            return await leaseweb_order_show(args.order_id)
        if args.subcommand == "cloud":
            if args.leaseweb_cloud == "doctor":
                return await leaseweb_cloud_doctor()
            if args.leaseweb_cloud == "accounts":
                return await leaseweb_cloud_accounts(args.action, args.account)
            if args.leaseweb_cloud == "catalog":
                return await leaseweb_cloud_catalog(args.region)
            if args.leaseweb_cloud == "create-preview":
                return await leaseweb_cloud_create_preview(
                    args.region,
                    args.type,
                    args.image,
                    args.reference,
                    args.root_disk_size,
                    args.root_disk_storage_type,
                )
            if args.leaseweb_cloud == "create":
                return await leaseweb_cloud_create(
                    args.user_id, args.offer_id, args.image_id, args.execute_live
                )
            print(f"unknown leaseweb cloud subcommand {args.leaseweb_cloud}")  # pragma: no cover
            return 2
        if args.subcommand == "vps":
            if args.leaseweb_vps == "list":
                return await leaseweb_vps_list()
            if args.leaseweb_vps == "show":
                return await leaseweb_vps_show(args.vps_id)
            if args.leaseweb_vps == "ips":
                return await leaseweb_vps_ips(args.vps_id)
            if args.leaseweb_vps == "metrics":
                return await leaseweb_vps_metrics(args)
            if args.leaseweb_vps == "snapshots":
                return await leaseweb_vps_snapshots(args.vps_id)
            return await leaseweb_vps_monitoring(args.vps_id)
        print(f"unknown leaseweb subcommand {args.subcommand}")  # pragma: no cover
        return 2
    if args.command == "hetzner":
        if args.subcommand == "doctor":
            result = await hetzner_doctor()
            print("\n".join(result.lines))
            return 0 if result.ok else 1
        if args.subcommand == "sync-offers":
            return await hetzner_sync_offers()
        print(f"unknown hetzner subcommand {args.subcommand}")  # pragma: no cover
        return 2
    if args.command == "offers":
        if args.subcommand == "list":
            return await offers_list(args.all)
        if args.subcommand == "doctor":
            result = await offers_doctor()
            print("\n".join(result.lines))
            return 0 if result.ok else 1
        if args.subcommand == "readiness":
            return await offers_readiness()
        if args.subcommand == "preview":
            return await offers_preview(args.market)
        if args.subcommand == "price-book":
            return await offers_price_book(
                args.provider,
                args.markup_percent,
                args.dry_run,
                args.include_disabled,
            )
        if args.subcommand == "price":
            return await offers_set(args.offer_id, "price", str(args.minor), args.currency)
        if args.subcommand == "normalize-selling-currency":
            return await offers_normalize_selling_currency(args.dry_run, args.target)
        return await offers_set(args.offer_id, args.subcommand, None, None)
    if args.command == "config":
        if args.subcommand == "doctor":
            return await config_doctor()
        print(f"unknown command config {args.subcommand}")  # pragma: no cover
        return 2
    if args.command == "catalog":
        if args.subcommand == "auto-sync" and args.subsubcommand == "doctor":
            return await catalog_auto_sync_doctor()
        if args.subcommand == "auto-sync" and args.subsubcommand == "run":
            return await catalog_auto_sync_run()
        print(f"unknown command catalog {args.subcommand}")  # pragma: no cover
        return 2
    if args.command == "users":
        return await users_find(args.telegram_id)
    if args.command == "wallet":
        if args.subcommand == "balance":
            return await wallet_balance(args.user_id)
        amount = args.minor if args.subcommand == "credit" else -abs(args.minor)
        return await wallet_adjust(args.user_id, amount, args.reason)
    if args.command == "orders":
        if args.subcommand == "list":
            return await orders_list(False)
        if args.subcommand == "attention":
            return await orders_list(True)
        if args.subcommand == "inspect":
            return await orders_inspect(args.order_id)
        if args.subcommand == "retry":
            return await orders_retry(args.order_id, args.reason)
        if args.subcommand == "resolve-existing":
            return await orders_resolve_existing(
                args.order_id, args.provider_order_id, args.reason, args.yes
            )
        if args.subcommand == "resolve-vps":
            return await orders_resolve_vps(args.order_id, args.vps_id, args.reason, args.yes)
        if args.subcommand == "resolve-not-created":
            return await orders_resolve_not_created(args.order_id, args.reason, args.yes)
    if args.command == "renewals":
        if args.subcommand == "list":
            return await renewals_list(args.attention)
        if args.subcommand == "check":
            return await renewals_check()
    print(f"unknown command {args.command}")  # pragma: no cover
    return 2


def main() -> int:
    args = _parser().parse_args()
    return asyncio.run(_dispatch(args))


if __name__ == "__main__":
    sys.exit(main())

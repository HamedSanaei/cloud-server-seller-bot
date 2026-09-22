"""Operator CLI for the LEASEWEB-MVP (and the pieces around it).

Run with::

    uv run python -m cloud_platform.cli <command> ...

Commands::

    leaseweb doctor              Pre-flight diagnostics (read-only; never
    leaseweb accounts list       Safe credential-account inventory (no keys)
    leaseweb accounts doctor     Per-account authentication pre-flight
    fx doctor                    Currency/FX pre-flight (AbanTether, cache)
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
        note("SKIP", "FX provider", "disabled ([fx] enabled = false)")
        return DoctorResult(True, lines)
    provider = (settings.fx_provider or "").strip().lower()
    if provider != "abantether":
        report("FX provider", False, f"unknown provider {provider!r}")
        return DoctorResult(ok, lines)
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
        if settings.fx_allow_usdt_proxy_for_display or settings.fx_allow_usdt_proxy_for_settlement:
            try:
                usdt = await client.get_quote(settings.fx_abantether_usd_proxy_symbol, "IRT")
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
                await cache.get("EUR->IRT")
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
        print("  a provider cost is not a selling price by design; set yours:")
        print(
            f"    python -m cloud_platform.cli offers price-book "
            f"--provider {provider_key} --markup-percent 30"
        )
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
        GATE_DISABLED,
        GATE_PROVIDER_UNAVAILABLE,
        GATE_UNPRICED,
        blocking_gate,
    )

    flags = {
        None: "SALE",
        GATE_DISABLED: "off",
        GATE_UNPRICED: "no-price",
        GATE_PROVIDER_UNAVAILABLE: "unavail",
    }
    for offer in sorted(offers, key=lambda o: (o.location_id, o.product_id)):
        flag = flags[blocking_gate(offer)]
        print(
            f"{offer.id}  {flag:8s}  {offer.location_id:8s} {offer.product_id:12s} "
            f"{offer.name:24s} cost={offer.provider_cost_minor} {offer.provider_cost_currency} "
            f"price={offer.selling_price_minor} {offer.selling_currency}"
        )
    return 0


def _offer_gate_counts(offers: list[Any]) -> dict[str, int]:
    """Count offers per blocking gate for one provider (pure domain helper)."""
    from cloud_platform.modules.offers.domain import visibility_summary

    return visibility_summary(offers)


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
        counts = _offer_gate_counts(by_provider.get(provider_key, []))
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
                f"       Action: python -m cloud_platform.cli offers price-book "
                f"--provider {provider_key} --markup-percent 30"
            )

    # 2) what the bot would actually render for each market
    for market in MARKET_ORDER:
        market_providers = markets_shown.get(market.value, [])
        has_sellable = [
            key
            for key in market_providers
            if _offer_gate_counts(by_provider.get(key, []))["sellable"]
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
        f"provider-unavailable={counts['provider_unavailable']}"
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

    sellable: dict[str, int] = {}
    stored: dict[str, int] = {}
    for row in rows:
        stored[row.provider_key] = stored.get(row.provider_key, 0) + 1
        if row.sellable:
            sellable[row.provider_key] = sellable.get(row.provider_key, 0) + 1

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
    print("")
    for provider_key in sorted(set(stored) | set(states) | set(policies) | set(credentials)):
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


async def offers_price_book(
    provider: str,
    markup_percent: int,
    dry_run: bool,
    include_disabled: bool,
) -> int:
    """Bulk-price every UNPRICED offer of one provider from its synced cost.

    The operator supplies the markup explicitly — this never invents a price,
    never reprices an offer that already has one, and never crosses
    currencies: the selling price is denominated in the SAME currency the
    provider cost was captured in, because relabelling without an explicit FX
    policy would be an implicit conversion. Integer minor units throughout.
    """
    from cloud_platform.db.session import SessionFactory
    from cloud_platform.modules.offers.domain import markup_unit_price
    from cloud_platform.modules.offers.repository import SqlAlchemySellableOfferRepository

    if markup_percent < 0:
        print("markup-percent must not be negative")
        return 2

    repo = SqlAlchemySellableOfferRepository(SessionFactory)
    rows = [row for row in await repo.list_all() if row.provider_key == provider]
    if not rows:
        print(f"no stored offers for provider {provider!r} — sync the catalog first")
        return 1

    priced = 0
    skipped_priced = 0
    skipped_disabled = 0
    skipped_cost = 0
    for row in sorted(rows, key=lambda o: (o.location_id, o.product_id)):
        if row.selling_price_minor > 0:
            skipped_priced += 1
            continue
        if not row.enabled and not include_disabled:
            # A disabled offer is hidden by the operator; pricing it would
            # imply an intent they did not express.
            skipped_disabled += 1
            continue
        if row.provider_cost_minor <= 0:
            skipped_cost += 1
            print(f"  warning: {row.ref} has no usable provider cost; left unpriced")
            continue
        try:
            price = markup_unit_price(row.provider_cost_minor, markup_percent)
        except ValueError as exc:  # pragma: no cover - guarded above
            print(f"  warning: {row.ref}: {exc}")
            skipped_cost += 1
            continue
        currency = row.provider_cost_currency
        if dry_run:
            print(
                f"  would price {row.ref}: cost {row.provider_cost_minor} "
                f"{currency} -> {price} {currency} (+{markup_percent}%)"
            )
            priced += 1
            continue
        await repo.set_selling_price(row.id, price, currency)
        # Operator-invoked bulk pricing is manual: the automatic policy must
        # never overwrite a price the operator chose explicitly.
        await repo.set_auto_priced(row.id, False)
        print(
            f"priced {row.ref}: {price} {currency} "
            f"(cost {row.provider_cost_minor}, +{markup_percent}%)"
        )
        priced += 1

    verb = "would price" if dry_run else "priced"
    print(
        f"{verb} {priced} offer(s); already priced: {skipped_priced}, "
        f"disabled (use --include-disabled): {skipped_disabled}, "
        f"no cost: {skipped_cost}"
    )
    if not dry_run and priced:
        print("next: python -m cloud_platform.cli offers doctor")
    return 0


async def offers_set(offer_id: str, action: str, price: str | None, currency: str | None) -> int:
    from cloud_platform.db.session import SessionFactory
    from cloud_platform.modules.offers.repository import SqlAlchemySellableOfferRepository

    repo = SqlAlchemySellableOfferRepository(SessionFactory)
    try:
        if action == "enable":
            # Explicit enable clears the operator block so the offer may sell.
            offer = await repo.set_enabled(UUID(offer_id), True)
            offer = await repo.set_operator_disabled(UUID(offer_id), False)
            print(f"enabled {offer.ref}")
        elif action == "disable":
            # Explicit disable persists an operator block that automatic
            # publishing never undoes (only another enable clears it).
            offer = await repo.set_enabled(UUID(offer_id), False)
            offer = await repo.set_operator_disabled(UUID(offer_id), True)
            print(f"disabled {offer.ref} (operator block recorded)")
        elif action == "price":
            if price is None:
                print("price requires a minor-unit amount")
                return 2
            offer = await repo.set_selling_price(UUID(offer_id), int(price), currency or "EUR")
            # A manually set price opts out of the automatic pricing policy.
            offer = await repo.set_auto_priced(UUID(offer_id), False)
            print(f"priced {offer.ref}: {offer.selling_price_minor} {offer.selling_currency}")
        else:  # pragma: no cover
            print(f"unknown action {action}")
            return 2
    except Exception as exc:
        print(f"error: {exc}")
        return 1
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
    price.add_argument("currency", nargs="?", default="EUR")

    catalog = sub.add_parser("catalog", help="automatic catalog sync operations")
    catalog_sub = catalog.add_subparsers(dest="subcommand", required=True)
    auto_sync = catalog_sub.add_parser("auto-sync", help="periodic offer refresh")
    auto_sync_sub = auto_sync.add_subparsers(dest="subsubcommand", required=True)
    auto_sync_sub.add_parser("doctor", help="read-only sync configuration and status")

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

    return parser


async def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "fx":
        if args.subcommand == "doctor":
            result = await fx_doctor()
            print("\n".join(result.lines))
            return 0 if result.ok else 1
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
        return await offers_set(args.offer_id, args.subcommand, None, None)
    if args.command == "catalog":
        if args.subcommand == "auto-sync" and args.subsubcommand == "doctor":
            return await catalog_auto_sync_doctor()
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

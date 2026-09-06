"""Operator CLI for the LEASEWEB-MVP (and the pieces around it).

Run with::

    uv run python -m cloud_platform.cli <command> ...

Commands::

    leaseweb doctor              Pre-flight diagnostics (read-only; never
                                 prints the API key, never orders anything)
    leaseweb sync-offers         Refresh the sellable-offer price book from
                                 the Leaseweb ordering API
    leaseweb smoke-order         Place a REAL order — requires
                                 LEASEWEB_ALLOW_LIVE_ORDER_TEST=true AND
                                 --yes; every other path refuses
    offers list [--all]          Sellable offers (or all rows)
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
    orders retry <order_id>      Reopen a FAILED order intent for the worker
                                 (same deterministic operation key -> safe)
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
        from redis.asyncio import from_url

        client = from_url(get_settings().redis_url)  # type: ignore[no-untyped-call]
        try:
            await client.ping()
        finally:
            await client.aclose()
        return True, "ok"
    except Exception as exc:
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

    if not settings.leaseweb_api_key:
        report("LEASEWEB_API_KEY configured", False, "set it in .env or the environment")
        lines.append("\nAction: add LEASEWEB_API_KEY=<key> to .env (from your Leaseweb portal).")
        return DoctorResult(ok, lines)
    report("LEASEWEB_API_KEY configured", True, f"set ({_redact(settings.leaseweb_api_key)})")

    from cloud_platform.providers.leaseweb.ordering import LeaseWebOrderingProvider

    provider = LeaseWebOrderingProvider(
        api_key=settings.leaseweb_api_key,
        base_url=settings.leaseweb_api_base_url,
        locations=tuple(
            part.strip() for part in (settings.leaseweb_locations or "").split(",") if part.strip()
        )
        or ("AMS-01", "FRA-01"),
        os_allowlist=_csv(settings.leaseweb_os_allowlist),
        order_os_only_free=settings.leaseweb_order_os_only_free,
    )
    try:
        locations = await provider.list_locations()
    except Exception as exc:
        report("Ordering API reachable", False, str(exc))
        report(
            "Authentication works",
            False,
            "X-LSW-Auth rejected — check the API key, or that your account is "
            "eligible for the VPS Ordering API (post-payment enabled). "
            "See docs/leaseweb/INTEGRATION_NOTES.md.",
        )
        return DoctorResult(ok, lines)
    report(
        "Ordering API reachable",
        True,
        f"{len(locations)} configured location(s): {', '.join(loc.id for loc in locations)}",
    )
    report("Authentication works", True, "products endpoint accepted X-LSW-Auth")

    from cloud_platform.db.session import SessionFactory
    from cloud_platform.modules.offers.repository import SqlAlchemySellableOfferRepository

    offers_repo = SqlAlchemySellableOfferRepository(SessionFactory)
    total_products = 0
    for loc in locations:
        try:
            products = await provider.list_products(loc.id)
        except Exception as exc:
            report(f"Products at {loc.id}", False, str(exc))
            continue
        total_products += len(products)
        report(f"Products at {loc.id}", True, f"{len(products)} product(s) returned")
        if not products:
            continue
        first = products[0]
        try:
            detail = await provider.get_product(loc.id, first.id)
        except Exception as exc:
            report(
                f"Product detail {first.id}@{loc.id}",
                False,
                f"price/options retrieval failed: {exc}",
            )
            continue
        free_os = len(detail.free_os_options())
        report(
            f"Product detail {first.id}@{loc.id}",
            True,
            f"monthly {detail.product.monthly_price_minor} {detail.product.currency}; "
            f"{len(detail.os_options)} OS option(s), {free_os} free",
        )
    if total_products == 0:
        report(
            "Products reported by the ordering API",
            False,
            "no products returned for the configured locations — the account may "
            "not be eligible for the VPS Ordering API yet",
        )
        lines.append(
            "\nAction: verify eligibility in the Leaseweb Customer Portal "
            "(ordering/VPS must be post-payment enabled)."
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

    if not ok:
        lines.append(
            "\nFix the FAIL items above, then re-run: "
            "uv run python -m cloud_platform.cli leaseweb doctor"
        )
    return DoctorResult(ok, lines)


# ---------------------------------------------------------------------------
# leaseweb sync-offers
# ---------------------------------------------------------------------------


async def leaseweb_sync_offers() -> int:
    from cloud_platform.db.session import SessionFactory
    from cloud_platform.providers.leaseweb.ordering import LeaseWebOrderingProvider
    from cloud_platform.providers.leaseweb.ordering_sync import LeaseWebOrderingCatalogSyncer

    settings = get_settings()
    if not settings.leaseweb_api_key:
        print("LEASEWEB_API_KEY is not set; cannot sync.")
        return 1
    provider = LeaseWebOrderingProvider(
        api_key=settings.leaseweb_api_key,
        base_url=settings.leaseweb_api_base_url,
        locations=tuple(
            part.strip() for part in (settings.leaseweb_locations or "").split(",") if part.strip()
        )
        or ("AMS-01", "FRA-01"),
        os_allowlist=_csv(settings.leaseweb_os_allowlist),
        order_os_only_free=settings.leaseweb_order_os_only_free,
    )
    syncer = LeaseWebOrderingCatalogSyncer(SessionFactory, provider)
    result = await syncer.sync_all()
    for name, step in result.items():
        print(
            f"{name}: fetched={step.total_fetched} upserted={step.total_upserted} "
            f"skipped={step.total_skipped}"
        )
        for error in step.errors:
            print(f"  error: {error}")
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
        print("no offers" + ("" if include_all else " — sync and enable+price first"))
        return 0
    for offer in sorted(offers, key=lambda o: (o.location_id, o.product_id)):
        if offer.sellable:
            flag = "SALE"
        elif not offer.enabled:
            flag = "off"
        elif offer.selling_price_minor <= 0:
            flag = "no-price"
        else:
            flag = "unavail"
        print(
            f"{offer.id}  {flag:8s}  {offer.location_id:8s} {offer.product_id:12s} "
            f"{offer.name:24s} cost={offer.provider_cost_minor} {offer.provider_cost_currency} "
            f"price={offer.selling_price_minor} {offer.selling_currency}"
        )
    return 0


async def offers_set(offer_id: str, action: str, price: str | None, currency: str | None) -> int:
    from cloud_platform.db.session import SessionFactory
    from cloud_platform.modules.offers.repository import SqlAlchemySellableOfferRepository

    repo = SqlAlchemySellableOfferRepository(SessionFactory)
    try:
        if action == "enable":
            offer = await repo.set_enabled(UUID(offer_id), True)
            print(f"enabled {offer.ref}")
        elif action == "disable":
            offer = await repo.set_enabled(UUID(offer_id), False)
            print(f"disabled {offer.ref}")
        elif action == "price":
            if price is None:
                print("price requires a minor-unit amount")
                return 2
            offer = await repo.set_selling_price(UUID(offer_id), int(price), currency or "EUR")
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


async def orders_retry(order_id: str) -> int:
    from cloud_platform.db.session import SessionFactory
    from cloud_platform.modules.operations.repository import SqlAlchemyOperationRepository
    from cloud_platform.modules.orders.repository import SqlAlchemyProviderOrderRepository

    orders_repo = SqlAlchemyProviderOrderRepository(SessionFactory)
    order = await orders_repo.get(UUID(order_id))
    if order is None:
        print(f"order {order_id} not found")
        return 1
    if order.status.value not in ("failed", "needs_review"):
        print(f"order {order_id} is {order.status.value}; only failed/needs_review may be retried")
        return 2
    op_repo = SqlAlchemyOperationRepository(SessionFactory)
    op = await op_repo.get_by_key(order.operation_key)
    if op is None:
        print(f"operation for order {order_id} missing; run checkout again")
        return 1
    if op.status.value == "failed":
        op.reopen_for_retry()
        await op_repo.save(op)
    print(
        f"order {order_id} reopened (operation {op.id} status={op.status.value}); "
        f"the worker will re-attempt with the SAME idempotency key — no duplicate order is possible"
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
# leaseweb smoke-order — real order ONLY with explicit env switch + --yes
# ---------------------------------------------------------------------------


async def leaseweb_smoke_order(offer_id: str, os_index: int) -> int:
    settings = get_settings()
    if not settings.leaseweb_allow_live_order_test:
        print(
            "REFUSED: placing a real Leaseweb order requires "
            "LEASEWEB_ALLOW_LIVE_ORDER_TEST=true (explicit environment switch).\n"
            "This is a BILLABLE action. See docs/operations/RUNBOOK.md for the "
            "controlled procedure."
        )
        return 2
    print("WARNING: this places a REAL, billable Leaseweb VPS order.")
    from cloud_platform.core.idempotency import IdempotencyKey
    from cloud_platform.db.session import SessionFactory
    from cloud_platform.modules.offers.repository import SqlAlchemySellableOfferRepository
    from cloud_platform.providers.base import CreateServerRequest
    from cloud_platform.providers.leaseweb.ordering import LeaseWebOrderingProvider

    repo = SqlAlchemySellableOfferRepository(SessionFactory)
    offer = await repo.get(UUID(offer_id))
    if offer is None or not offer.sellable:
        print(f"offer {offer_id} is not sellable; aborting")
        return 1
    provider = LeaseWebOrderingProvider(
        api_key=settings.leaseweb_api_key,
        base_url=settings.leaseweb_api_base_url,
        locations=(offer.location_id,),
    )
    detail = await provider.get_product(offer.location_id, offer.product_id)
    options = (
        detail.free_os_options() if settings.leaseweb_order_os_only_free else detail.os_options
    )
    if os_index < 0 or os_index >= len(options):
        print(f"OS index {os_index} out of range (0..{len(options) - 1})")
        return 1
    os_name = options[os_index].name
    print(
        f"offer={offer.ref} os={os_name} monthly={offer.selling_price_minor} "
        f"{offer.selling_currency}"
    )
    from uuid import uuid4

    key = f"smoke-order:{uuid4()}"
    ticket = await provider.place_order(
        CreateServerRequest(
            name=f"smoke-{key[-8:]}",
            plan_id=offer.product_id,
            image_id=os_name,
            location_id=offer.location_id,
            labels={"price_minor": str(offer.selling_price_minor), "smoke": "1"},
        ),
        IdempotencyKey(key),
    )
    print(f"ORDER PLACED: provider_order_id={ticket.provider_order_id} state={ticket.state}")
    print(
        "Track it: uv run python -m cloud_platform.cli orders inspect "
        "(after the checkout flow) or the Leaseweb portal. "
        "This order is NOT linked to a platform server."
    )
    return 0


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cloud_platform.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    lsw = sub.add_parser("leaseweb", help="Leaseweb ordering operations")
    lsw_sub = lsw.add_subparsers(dest="subcommand", required=True)
    lsw_sub.add_parser("doctor", help="read-only pre-flight diagnostics")
    lsw_sub.add_parser("sync-offers", help="refresh the sellable-offer price book")
    smoke = lsw_sub.add_parser("smoke-order", help="REAL billable order (guarded)")
    smoke.add_argument("--offer", required=True, help="sellable offer id")
    smoke.add_argument("--os-index", type=int, default=0, help="OS option index")
    smoke.add_argument("--yes", action="store_true", help="confirm the billable action")

    offers = sub.add_parser("offers", help="sellable-offer management")
    offers_sub = offers.add_subparsers(dest="subcommand", required=True)
    offers_list_p = offers_sub.add_parser("list")
    offers_list_p.add_argument("--all", action="store_true", help="include disabled/unpriced")
    for action in ("enable", "disable"):
        p = offers_sub.add_parser(action)
        p.add_argument("offer_id")
    price = offers_sub.add_parser("price")
    price.add_argument("offer_id")
    price.add_argument("minor", type=int, help="selling price in minor units (e.g. 1299 = 12.99)")
    price.add_argument("currency", nargs="?", default="EUR")

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
    retry = orders_sub.add_parser("retry")
    retry.add_argument("order_id")

    renewals = sub.add_parser("renewals")
    renewals_sub = renewals.add_subparsers(dest="subcommand", required=True)
    rl = renewals_sub.add_parser("list")
    rl.add_argument("--attention", action="store_true")
    renewals_sub.add_parser("check")

    return parser


async def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "leaseweb":
        if args.subcommand == "doctor":
            result = await leaseweb_doctor()
            print("\n".join(result.lines))
            return 0 if result.ok else 1
        if args.subcommand == "sync-offers":
            return await leaseweb_sync_offers()
        if args.subcommand == "smoke-order":
            if not args.yes:
                print(
                    "REFUSED: pass --yes to confirm. This is a BILLABLE action and "
                    "requires LEASEWEB_ALLOW_LIVE_ORDER_TEST=true."
                )
                return 2
            return await leaseweb_smoke_order(args.offer, args.os_index)
        print(f"unknown leaseweb subcommand {args.subcommand}")  # pragma: no cover
        return 2
    if args.command == "offers":
        if args.subcommand == "list":
            return await offers_list(args.all)
        if args.subcommand == "price":
            return await offers_set(args.offer_id, "price", str(args.minor), args.currency)
        return await offers_set(args.offer_id, args.subcommand, None, None)
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
            return await orders_retry(args.order_id)
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

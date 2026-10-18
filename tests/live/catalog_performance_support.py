"""Isolated loopback scratch schemas for catalog tests/benchmarks, no app DB fallback."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import MetaData, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import DefaultClause

from cloud_platform.db.base import Base, ProviderRoute, SellableOffer
from cloud_platform.modules.offers.domain import OfferSpecUpdate


@asynccontextmanager
async def scratch_catalog(*, pool_size: int = 1) -> AsyncIterator[tuple[Any, Any]]:
    raw = os.environ.get("CLOUD_PLATFORM_TEST_POSTGRES_URL", "")
    if not raw:
        raise RuntimeError("explicit scratch PostgreSQL URL required")
    url = make_url(raw)
    if url.drivername != "postgresql+asyncpg" or url.host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("only explicit loopback scratch PostgreSQL is permitted")
    schema = "catalog_perf_" + uuid4().hex
    engine = create_async_engine(
        url,
        pool_size=pool_size,
        max_overflow=0,
        hide_parameters=True,
        connect_args={
            "server_settings": {
                "search_path": schema,
                "statement_timeout": "10000",
                "lock_timeout": "3000",
            }
        },
    )
    metadata = MetaData()

    def include(table: Any) -> None:
        if table.name in metadata.tables:
            return
        table.to_metadata(metadata)
        for foreign in table.foreign_keys:
            include(Base.metadata.tables[foreign.column.table.name])

    include(SellableOffer.__table__)
    include(ProviderRoute.__table__)
    for table in metadata.tables.values():
        for column in table.columns:
            if column.server_default is not None and "uuid_generate_v4" in str(
                column.server_default.arg
            ):
                column.server_default = DefaultClause(text("gen_random_uuid()"))
            elif (
                column.server_default is not None
                and str(column.server_default.arg) == "CURRENT_TIMESTAMP"
            ):
                column.server_default = DefaultClause(text("CURRENT_TIMESTAMP"))
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            await connection.run_sync(metadata.create_all)
        yield engine, async_sessionmaker(engine, expire_on_commit=False)
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


def observation(index: int, *, price: int = 100, account: str = "account-a") -> dict[str, Any]:
    return dict(
        provider_key="leaseweb",
        product_id=f"plan-{index:05}",
        location_id="AMS-01",
        provider_account_id=account,
        update=OfferSpecUpdate(
            name=f"Plan {index}",
            vcpu=2,
            ram_gb=4,
            disk_gb=40,
            traffic="1 TB",
            provider_cost_minor=price,
            provider_cost_currency="USD",
            provider_available=True,
            billing_model="hourly",
            billing_parameters={"provider_hourly_rate": str(Decimal(price) / 100)},
        ),
    )

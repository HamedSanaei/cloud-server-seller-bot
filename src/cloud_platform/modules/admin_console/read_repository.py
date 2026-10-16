"""Bounded local catalog and aggregate operation/queue health reads."""

from sqlalchemy import func, select

from cloud_platform.db.base import AdminMessageDelivery, Operation, SellableOffer, Server
from cloud_platform.modules.offers.repository import _to_domain


class SqlAlchemyAdminReadRepository:
    def __init__(self, session_factory, *, dependency_probe=None):
        self._sessions = session_factory
        self._probe = dependency_probe

    async def list_offers(self, *, offset=0, limit=10):
        if (
            type(offset) is not int
            or not 0 <= offset <= 10000
            or type(limit) is not int
            or not 1 <= limit <= 20
        ):
            raise ValueError("catalog page out of bounds")
        async with self._sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(SellableOffer)
                        .order_by(SellableOffer.provider_key, SellableOffer.id)
                        .offset(offset)
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
            return tuple(_to_domain(row) for row in rows)

    async def health(self):
        async with self._sessions() as session:
            await session.execute(select(1))
            operations = (
                await session.execute(
                    select(Operation.status, func.count()).group_by(Operation.status)
                )
            ).all()
            messages = (
                await session.execute(
                    select(AdminMessageDelivery.status, func.count()).group_by(
                        AdminMessageDelivery.status
                    )
                )
            ).all()
            servers = (
                await session.execute(select(Server.state, func.count()).group_by(Server.state))
            ).all()
            dependencies = await self._probe() if self._probe is not None else {}
            return {
                "database": "ok",
                "dependencies": dependencies,
                "servers": dict(servers),
                "operations": dict(operations),
                "message_deliveries": dict(messages),
            }

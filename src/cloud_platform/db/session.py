from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from cloud_platform.core.config import get_settings
from cloud_platform.observability.catalog import install_sql_instrumentation

_settings = get_settings()
_engine = create_async_engine(_settings.database_url, pool_pre_ping=True, hide_parameters=True)
SessionFactory = async_sessionmaker(_engine, expire_on_commit=False)

install_sql_instrumentation(_engine)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session

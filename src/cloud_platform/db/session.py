from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from cloud_platform.core.config import get_settings

_settings = get_settings()
_engine = create_async_engine(_settings.database_url, pool_pre_ping=True)
SessionFactory = async_sessionmaker(_engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionFactory() as session:
        yield session

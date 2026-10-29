from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool

from app.config import settings


class Base(DeclarativeBase):
    pass


engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


@asynccontextmanager
async def worker_session() -> AsyncIterator[AsyncSession]:
    """Сессия для Celery-задач: каждая задача крутит свой event loop, поэтому без пула."""
    eng = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(eng, expire_on_commit=False)() as session:
            yield session
    finally:
        await eng.dispose()

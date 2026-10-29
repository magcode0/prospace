import asyncio
import os

# тесты работают в отдельной БД; задаём до импорта приложения
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://prospace:prospace@localhost:5433/prospace_test")


import asyncpg  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app import models  # noqa: E402,F401
from app.config import settings  # noqa: E402
from app.db import Base  # noqa: E402

CATEGORIES = [("web", "Веб-разработка"), ("design", "Дизайн")]


async def _recreate_db() -> None:
    dsn = settings.database_url.replace("+asyncpg", "")
    admin_dsn, name = dsn.rsplit("/", 1)
    conn = await asyncpg.connect(f"{admin_dsn}/postgres")
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(models.Category.__table__.insert(), [{"slug": s, "name": n} for s, n in CATEGORIES])
    await engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def database():
    asyncio.run(_recreate_db())


@pytest.fixture
async def session():
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


@pytest.fixture(autouse=True)
async def fresh_redis():
    # клиент redis.asyncio привязан к loop'у — на каждый тест свой
    from redis.asyncio import Redis

    from app.redis import set_redis

    client = Redis.from_url(settings.redis_url, decode_responses=True)
    set_redis(client)
    yield client
    set_redis(None)
    await client.aclose()


@pytest.fixture(autouse=True)
def no_email(monkeypatch):
    sent = []

    async def fake_send(to, subject, body):
        sent.append((to, subject, body))

    monkeypatch.setattr("app.services.notifications.send_email", fake_send)
    monkeypatch.setattr("app.services.mail.send_email", fake_send)
    return sent

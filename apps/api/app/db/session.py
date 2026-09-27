from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.settings import Settings


def async_database_url(database_url: str) -> str:
    """Return an asyncpg URL while accepting the usual PostgreSQL URL form."""
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return database_url


def create_engine(settings: Settings) -> AsyncEngine | None:
    if not settings.database_url:
        return None
    url = async_database_url(settings.database_url)
    options: dict[str, object] = {
        "pool_pre_ping": True,
        "pool_recycle": 1800,
    }
    if url.startswith("postgresql+"):
        options["connect_args"] = {"timeout": 2}
    return create_async_engine(url, **options)


def session_factory(
    settings: Settings,
) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]] | None:
    engine = create_engine(settings)
    if engine is None:
        return None
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def check_database(settings: Settings) -> str:
    """Check the configured database without making readiness fail noisily."""
    if not settings.database_url:
        return "not_configured"
    try:
        import asyncpg  # type: ignore[import-not-found]  # optional until configured
    except ImportError:
        return "driver_missing"
    del asyncpg
    engine: AsyncEngine | None = None
    try:
        engine = create_engine(settings)
        if engine is None:
            return "not_configured"
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return "ready"
    except (ImportError, SQLAlchemyError, OSError, TimeoutError):
        return "unavailable"
    finally:
        if engine is not None:
            await engine.dispose()


async def dispose_engine(engine: AsyncEngine | None) -> None:
    if engine is not None:
        await engine.dispose()


__all__ = [
    "AsyncEngine",
    "AsyncSession",
    "async_database_url",
    "check_database",
    "create_engine",
    "dispose_engine",
    "session_factory",
    "session_scope",
]

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

settings = get_settings()


def _build_async_url(url: str) -> str:
    """Convert sync DB URL to async-compatible driver."""
    if url.startswith("sqlite:///"):
        return url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    if "mssql+pyodbc" in url:
        return url.replace("mssql+pyodbc", "mssql+aioodbc", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


_sync_url = settings.effective_database_url
_async_url = _build_async_url(_sync_url)

_connect_args: dict = {}
if "sqlite" in _async_url:
    # timeout=60: aiosqlite waits up to 60s for a write lock (sqlite3.connect timeout param)
    # check_same_thread=False: required for async usage across tasks
    _connect_args = {"check_same_thread": False, "timeout": 60}

engine = create_async_engine(
    _async_url,
    echo=settings.app_debug,
    connect_args=_connect_args,
    pool_pre_ping=True,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


@asynccontextmanager
async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def create_tables() -> None:
    from app.db.models import Base

    async with engine.begin() as conn:
        # WAL mode: allows concurrent readers while one writer is active
        if "sqlite" in _async_url:
            await conn.execute(__import__("sqlalchemy", fromlist=["text"]).text("PRAGMA journal_mode=WAL"))
            await conn.execute(__import__("sqlalchemy", fromlist=["text"]).text("PRAGMA synchronous=NORMAL"))
            await conn.execute(__import__("sqlalchemy", fromlist=["text"]).text("PRAGMA busy_timeout=30000"))
        await conn.run_sync(Base.metadata.create_all)


async def drop_tables() -> None:
    from app.db.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

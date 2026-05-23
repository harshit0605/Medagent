from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from threading import Lock
from typing import Optional

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


_engine: Optional[AsyncEngine] = None
_SessionLocal: Optional[async_sessionmaker[AsyncSession]] = None
_lock = Lock()


def _build_engine() -> AsyncEngine:
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    # Pool sizing — split between prod and test profiles via
    # env vars so a deployment doesn't have to ship with
    # test-friendly defaults.
    #
    # Production: 5 + 5 overflow = 10 max. Plenty for the
    # FastAPI worker count (uvicorn 1-2 workers per pod) and
    # well under Supabase pgbouncer's transaction-mode pool
    # cap.
    #
    # Tests: bumped to 10 + 10 = 20 max. The full integration
    # suite spins up many module-scoped TestClient fixtures
    # that each open + hold connections during fixture
    # lifetime; the prod-default 10 starves quickly when 5+
    # files run sequentially in a single process. 20 stays
    # under Supabase's typical 30 cap while giving the suite
    # headroom for concurrent fixture lifetimes.
    pool_size = int(os.getenv("DB_POOL_SIZE", "5"))
    max_overflow = int(os.getenv("DB_MAX_OVERFLOW", "5"))
    return create_async_engine(
        url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        # Recycle connections every 5 min — Supabase pgbouncer
        # closes idle connections aggressively, and a long
        # test session can hold idle pooled connections that
        # have been server-side closed. Recycling on a timer
        # avoids the "stale connection" race where pre-ping
        # fires AFTER pgbouncer has already dropped us.
        pool_recycle=int(os.getenv("DB_POOL_RECYCLE", "300")),
        echo=os.getenv("SQL_ECHO") == "1",
        future=True,
        # Supabase's pooled URL runs PgBouncer in transaction mode, which
        # doesn't tolerate psycopg's auto-prepared statements (causes
        # `DuplicatePreparedStatement: "_pg3_0" already exists` errors).
        # `None` disables psycopg's prepared-statement cache entirely.
        connect_args={"prepare_threshold": None},
    )


def get_engine() -> AsyncEngine:
    global _engine, _SessionLocal
    if _engine is None:
        with _lock:
            if _engine is None:
                _engine = _build_engine()
                _SessionLocal = async_sessionmaker(
                    bind=_engine,
                    autoflush=False,
                    autocommit=False,
                    expire_on_commit=False,
                )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency. Yields an AsyncSession; commits on exit, rolls back on error."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

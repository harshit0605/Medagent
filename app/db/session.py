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
    # Production: 10 + 10 overflow = 20 max per process. Bumped from the
    # original 5/5 after a perf audit flagged pool starvation as a likely
    # cause of cascading timeouts under sustained load — with multiple
    # replicas + the scheduler process holding its own pool, 10 max per
    # process saturates pgbouncer's per-app pool fast. 20 still sits
    # comfortably under Supabase's typical 30-conn-per-app cap.
    #
    # Tests already pin DB_POOL_SIZE=10 / DB_MAX_OVERFLOW=10 explicitly
    # in conftest, so prod-default bumps don't change test behaviour.
    pool_size = int(os.getenv("DB_POOL_SIZE", "10"))
    max_overflow = int(os.getenv("DB_MAX_OVERFLOW", "10"))
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

"""DB connectivity smoke test — skipped when DATABASE_URL is unset."""

import os

import pytest
from sqlalchemy import text


pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping live DB smoke test",
)


async def test_engine_connects_and_runs_select_one():
    from app.db.session import get_engine

    engine = get_engine()
    async with engine.connect() as conn:
        result = await conn.execute(text("select 1"))
        assert result.scalar_one() == 1


async def test_session_yields_and_commits():
    from app.db.session import get_sessionmaker

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as session:
        result = await session.execute(text("select 1"))
        assert result.scalar_one() == 1

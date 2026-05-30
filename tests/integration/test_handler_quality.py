"""Per-handler reply-quality monitor (F6)."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.db.models import InboundClassification
from app.db.session import get_sessionmaker

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping integration tests",
)


@pytest.fixture(scope="module")
def client():
    from services.orchestrator.main import app

    with TestClient(app) as c:
        yield c


async def _seed_rated(handler: str, rating: int) -> None:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        row = InboundClassification(
            patient_phone=f"hq-{uuid.uuid4().hex[:8]}",
            category="clinical_question",
            urgency="low",
            handler_used=handler,
            feedback_rating=rating,
            feedback_at=datetime.now(timezone.utc),
            feedback_by="ops.test",
        )
        db.add(row)
        await db.commit()


def test_handler_quality_aggregates_by_handler(client):
    import asyncio

    token = uuid.uuid4().hex[:6]
    good = f"good_handler_{token}"
    bad = f"bad_handler_{token}"
    loop = asyncio.get_event_loop()
    # good: 3 up, 1 down → up_rate 0.75
    for _ in range(3):
        loop.run_until_complete(_seed_rated(good, 1))
    loop.run_until_complete(_seed_rated(good, -1))
    # bad: 1 up, 3 down → up_rate 0.25
    loop.run_until_complete(_seed_rated(bad, 1))
    for _ in range(3):
        loop.run_until_complete(_seed_rated(bad, -1))

    r = client.get("/ops/analytics/handler-quality?window_days=7")
    assert r.status_code == 200, r.text
    by_handler = {row["handler"]: row for row in r.json()}
    assert by_handler[good]["total_rated"] == 4
    assert by_handler[good]["thumbs_up"] == 3
    assert by_handler[good]["up_rate"] == 0.75
    assert by_handler[bad]["up_rate"] == 0.25

    # Worst up-rate sorts first → bad handler appears before good in the list.
    handlers_in_order = [row["handler"] for row in r.json()]
    assert handlers_in_order.index(bad) < handlers_in_order.index(good)


def test_handler_quality_window_validation(client):
    assert client.get("/ops/analytics/handler-quality?window_days=0").status_code == 400
    assert client.get("/ops/analytics/handler-quality?window_days=999").status_code == 400

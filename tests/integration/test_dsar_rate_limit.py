"""DSAR export per-operator rate limit + abuse alert.

A leaked credential could scrape the patient DB one export at a time; the
per-operator/24h ceiling caps that, returns 429 once tripped, and opens an
idempotent ops alert. Counted against the durable operator_actions log.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.db.models import Patient
from app.db.repositories import operator_actions as ops_audit
from app.db.repositories import ops_tickets as ops_tickets_repo
from app.db.session import get_sessionmaker

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping integration tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


@pytest.fixture
def low_limit(monkeypatch):
    monkeypatch.setenv("DSAR_EXPORT_DAILY_LIMIT", "2")
    yield 2


async def _seed_patient() -> int:
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(
            full_name=f"DSAR RL {suffix}",
            phone=f"dsarrl-{suffix}",
            consent_sms=True,
        )
        db.add(p)
        await db.flush()
        await db.commit()
        return p.id


def test_export_rate_limit_trips_and_alerts(orchestrator_client, low_limit):
    import asyncio

    pid = asyncio.get_event_loop().run_until_complete(_seed_patient())
    operator = f"rl-op-{uuid.uuid4().hex[:6]}"
    headers = {"X-Ops-Actor": operator}

    # First 2 exports succeed (limit=2, so the 1st and 2nd are under it).
    r1 = orchestrator_client.get(f"/patients/{pid}/export", headers=headers)
    assert r1.status_code == 200, r1.text
    r2 = orchestrator_client.get(f"/patients/{pid}/export", headers=headers)
    assert r2.status_code == 200, r2.text

    # 3rd export: 2 already logged in 24h >= limit 2 → 429.
    r3 = orchestrator_client.get(f"/patients/{pid}/export", headers=headers)
    assert r3.status_code == 429, r3.text
    assert "rate limit" in r3.json()["detail"].lower()

    # An abuse alert ticket was opened for this operator.
    SessionLocal = get_sessionmaker()

    async def _check():
        async with SessionLocal() as db:
            ticket = await ops_tickets_repo.find_open_for_patient_category(
                db,
                patient_id=f"platform:dsar:{operator}",
                category="dsar_export_abuse",
            )
            return ticket

    ticket = asyncio.get_event_loop().run_until_complete(_check())
    assert ticket is not None
    assert operator in (ticket.notes or "")

    # Cleanup the alert ticket.
    async def _cleanup():
        async with SessionLocal() as db:
            await ops_tickets_repo.resolve(db, ticket.id, actor="test")
            await db.commit()

    asyncio.get_event_loop().run_until_complete(_cleanup())


def test_disabled_when_limit_zero(orchestrator_client, monkeypatch):
    monkeypatch.setenv("DSAR_EXPORT_DAILY_LIMIT", "0")
    import asyncio

    pid = asyncio.get_event_loop().run_until_complete(_seed_patient())
    operator = f"rl-off-{uuid.uuid4().hex[:6]}"
    headers = {"X-Ops-Actor": operator}
    # Many exports, all succeed — limit disabled.
    for _ in range(5):
        r = orchestrator_client.get(f"/patients/{pid}/export", headers=headers)
        assert r.status_code == 200


async def test_count_recent_actions_helper():
    from datetime import datetime, timedelta, timezone

    operator = f"cnt-{uuid.uuid4().hex[:6]}"
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        for _ in range(3):
            await ops_audit.record(
                db,
                operator_id=operator,
                action=ops_audit.ACTION_PATIENT_EXPORT,
                target_type="patient",
                target_id="1",
            )
        await db.commit()

    async with SessionLocal() as db:
        n = await ops_audit.count_recent_actions(
            db,
            operator_id=operator,
            action=ops_audit.ACTION_PATIENT_EXPORT,
            since=datetime.now(timezone.utc) - timedelta(hours=24),
        )
    assert n == 3

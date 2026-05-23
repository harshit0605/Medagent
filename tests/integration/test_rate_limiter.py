"""Integration tests for the inbound rate-limit gate.

End-to-end against real Postgres because:
    1. The counter query runs against a real ``message_log`` table.
    2. The early-return path writes to ``audit_records``,
       ``message_log``, and ``ops_tickets`` — all real tables.
    3. Ticket idempotency depends on
       ``find_open_for_patient_category`` actually finding the
       previously-opened row in the same DB.

Skipped when DATABASE_URL is unset.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import AuditRecord, MessageDirection, MessageLog
from app.db.repositories import (
    message_log as message_log_repo,
    ops_tickets as ops_tickets_repo,
)
from app.db.session import get_sessionmaker
from services.orchestrator import rate_limiter

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping rate-limit integration tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


def _phone() -> str:
    """Per-test unique synthetic phone — the integration suite has
    no per-test isolation, so reusing a phone would conflate
    counts with rows seeded by other tests."""
    return f"rate-test-{uuid.uuid4().hex[:10]}"


async def _seed_inbound(*, phone: str, count: int, age_minutes: int = 1):
    """Drop ``count`` inbound message_log rows for ``phone``,
    backdated by ``age_minutes`` so they fall inside the rolling
    window."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        when = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
        for i in range(count):
            await message_log_repo.append_inbound(
                db,
                patient_id=phone,
                payload={"text": f"msg {i}"},
                occurred_at=when,
            )
        await db.commit()


# ---- Counter against a real message_log -----------------------------------


async def test_counter_returns_zero_for_unseen_patient():
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        out = await rate_limiter.check_inbound_rate_limit(
            db, patient_phone=_phone()
        )
        assert out.is_limited is False
        assert out.count == 0


async def test_counter_counts_recent_inbound():
    """Seed 5 inbound rows in-window → count returns 5; below
    default threshold of 30 so not limited."""
    phone = _phone()
    await _seed_inbound(phone=phone, count=5)
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        out = await rate_limiter.check_inbound_rate_limit(
            db, patient_phone=phone
        )
        assert out.count == 5
        assert out.is_limited is False


async def test_counter_excludes_rows_outside_window():
    """Old rows (15 min ago, with default 5-min window) must NOT
    count. Otherwise an old chatty patient would be permanently
    rate-limited."""
    phone = _phone()
    # 5 rows from 15 minutes ago — well outside default 5-min window.
    await _seed_inbound(phone=phone, count=5, age_minutes=15)
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        out = await rate_limiter.check_inbound_rate_limit(
            db, patient_phone=phone
        )
        assert out.count == 0


async def test_counter_fires_at_threshold(monkeypatch):
    """30 in-window rows → is_limited=True. Tighten the limit via
    env so we don't have to seed 30 rows; same code path tested
    with a smaller threshold."""
    monkeypatch.setenv("INBOUND_RATE_LIMIT_COUNT", "5")
    phone = _phone()
    await _seed_inbound(phone=phone, count=5)
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        out = await rate_limiter.check_inbound_rate_limit(
            db, patient_phone=phone
        )
        assert out.is_limited is True
        assert out.count == 5


# ---- /route endpoint round-trip ------------------------------------------


def test_route_endpoint_short_circuits_when_rate_limited(
    orchestrator_client, monkeypatch
):
    """End-to-end: pre-seed enough inbound rows that the gate
    fires, then POST /route. Response must include
    ``rate_limited=True`` AND skip the LLM/handler chain — the
    response body must be empty (no MessageOut to send)."""
    import asyncio

    monkeypatch.setenv("INBOUND_RATE_LIMIT_COUNT", "3")

    phone = _phone()
    asyncio.get_event_loop().run_until_complete(
        _seed_inbound(phone=phone, count=3)
    )

    response = orchestrator_client.post(
        "/route",
        json={
            "message": {
                "message_id": uuid.uuid4().hex,
                "patient_id": phone,
                "phone": phone,
                "text": "hello",
            }
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body.get("rate_limited") is True
    assert body.get("audit_reasons") == ["rate_limited"]
    assert body.get("flow_action") == "HOLD"
    # Empty MessageOut body — gateway will not send.
    assert body["message_out"]["body"] == ""
    assert body["message_out"]["use_template"] is False
    assert body["rate_limit_count"] == 3


def test_route_endpoint_audit_row_written_when_rate_limited(
    orchestrator_client, monkeypatch
):
    """Every rate-limited inbound writes an audit row with
    ``rate_limited`` reason — that's the forensic trail."""
    import asyncio

    monkeypatch.setenv("INBOUND_RATE_LIMIT_COUNT", "3")

    phone = _phone()
    loop = asyncio.get_event_loop()
    loop.run_until_complete(_seed_inbound(phone=phone, count=3))

    orchestrator_client.post(
        "/route",
        json={
            "message": {
                "message_id": uuid.uuid4().hex,
                "patient_id": phone,
                "phone": phone,
                "text": "hello",
            }
        },
    )

    async def _audit_for(phone_: str):
        SessionLocal = get_sessionmaker()
        async with SessionLocal() as db:
            stmt = (
                select(AuditRecord)
                .where(AuditRecord.patient_id == phone_)
                .where(AuditRecord.flow_action == "HOLD")
            )
            return list((await db.execute(stmt)).scalars().all())

    rows = loop.run_until_complete(_audit_for(phone))
    assert len(rows) >= 1
    assert "rate_limited" in (rows[-1].reason_codes or [])


def test_route_endpoint_opens_ticket_on_first_breach(
    orchestrator_client, monkeypatch
):
    """First rate-limit breach for a patient opens an
    ``inbound_rate_limit`` ops ticket. Pre-condition for the
    idempotency test below — confirm the ticket gets created."""
    import asyncio

    monkeypatch.setenv("INBOUND_RATE_LIMIT_COUNT", "3")

    phone = _phone()
    loop = asyncio.get_event_loop()
    loop.run_until_complete(_seed_inbound(phone=phone, count=3))

    orchestrator_client.post(
        "/route",
        json={
            "message": {
                "message_id": uuid.uuid4().hex,
                "patient_id": phone,
                "phone": phone,
                "text": "hello",
            }
        },
    )

    async def _tickets_for(phone_: str):
        SessionLocal = get_sessionmaker()
        async with SessionLocal() as db:
            return await ops_tickets_repo.list_for_patient_by_category(
                db, phone_, "inbound_rate_limit"
            )

    rows = loop.run_until_complete(_tickets_for(phone))
    assert len(rows) == 1
    assert rows[0].priority == "high"
    assert rows[0].sla_minutes == 60


def test_route_endpoint_ticket_is_idempotent_per_breach(
    orchestrator_client, monkeypatch
):
    """A sustained burst (multiple rate-limited inbounds) must
    NOT open a ticket per inbound — that would drown ops in
    duplicate alerts. Subsequent rate-limit hits with an open
    ticket leave the queue clean."""
    import asyncio

    monkeypatch.setenv("INBOUND_RATE_LIMIT_COUNT", "3")

    phone = _phone()
    loop = asyncio.get_event_loop()
    loop.run_until_complete(_seed_inbound(phone=phone, count=3))

    # Three inbound bursts within the same window — first opens
    # the ticket, second + third should NOT open additional ones.
    for _ in range(3):
        orchestrator_client.post(
            "/route",
            json={
                "message": {
                    "message_id": uuid.uuid4().hex,
                    "patient_id": phone,
                    "phone": phone,
                    "text": "burst",
                }
            },
        )

    async def _tickets_for(phone_: str):
        SessionLocal = get_sessionmaker()
        async with SessionLocal() as db:
            return await ops_tickets_repo.list_for_patient_by_category(
                db, phone_, "inbound_rate_limit"
            )

    rows = loop.run_until_complete(_tickets_for(phone))
    # Exactly ONE ticket regardless of how many inbounds were
    # rate-limited in the burst.
    assert len(rows) == 1


def test_route_endpoint_logs_inbound_for_forensics_when_rate_limited(
    orchestrator_client, monkeypatch
):
    """Even rate-limited inbounds get logged to ``message_log``
    — we want the forensic trail of what was sent during a
    burst, not just a count. The next inbound's counter sees
    them as part of the rolling window so the gate stays armed."""
    import asyncio

    monkeypatch.setenv("INBOUND_RATE_LIMIT_COUNT", "3")

    phone = _phone()
    loop = asyncio.get_event_loop()
    loop.run_until_complete(_seed_inbound(phone=phone, count=3))

    orchestrator_client.post(
        "/route",
        json={
            "message": {
                "message_id": uuid.uuid4().hex,
                "patient_id": phone,
                "phone": phone,
                "text": "blocked-but-logged",
            }
        },
    )

    async def _inbound_logs_for(phone_: str):
        SessionLocal = get_sessionmaker()
        async with SessionLocal() as db:
            stmt = (
                select(MessageLog)
                .where(MessageLog.patient_id == phone_)
                .where(MessageLog.direction == MessageDirection.inbound)
            )
            return list((await db.execute(stmt)).scalars().all())

    rows = loop.run_until_complete(_inbound_logs_for(phone))
    # 3 seeded + 1 rate-limited = 4 logged inbound rows.
    assert len(rows) == 4
    # The blocked one made it into the log with its actual payload.
    assert any(
        (row.message or {}).get("text") == "blocked-but-logged"
        for row in rows
    )

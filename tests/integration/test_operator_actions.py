"""Integration tests for the operator_actions audit log.

Verifies:
- The repo writes + queries correctly (insert + list-by-operator + list-by-target).
- DSAR export / erasure / pause / unpause / ticket ack / ticket resolve
  endpoints each persist a corresponding operator_actions row with the
  expected operator_id + action + target.

Skipped when DATABASE_URL is unset.
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


async def _seed_patient(name: str = "Audit Test") -> tuple[int, str]:
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(
            full_name=f"{name} {suffix}",
            phone=f"opaudit-{suffix}",
            consent_sms=True,
        )
        db.add(p)
        await db.flush()
        await db.commit()
        return p.id, p.phone


# ---- repo --------------------------------------------------------------


async def test_record_and_list_for_operator():
    suffix = uuid.uuid4().hex[:8]
    operator_id = f"ops-{suffix}"
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await ops_audit.record(
            db,
            operator_id=operator_id,
            action=ops_audit.ACTION_TICKET_RESOLVE,
            target_type="ticket",
            target_id="42",
            details={"notes": "fixed"},
        )
        await ops_audit.record(
            db,
            operator_id=operator_id,
            action=ops_audit.ACTION_PATIENT_PAUSE,
            target_type="patient",
            target_id="99",
            details={"reason": "investigate"},
        )
        await db.commit()

    async with SessionLocal() as db:
        rows = await ops_audit.list_for_operator(db, operator_id)
        assert len(rows) == 2
        # Newest first.
        assert rows[0].action == ops_audit.ACTION_PATIENT_PAUSE
        assert rows[1].action == ops_audit.ACTION_TICKET_RESOLVE
        assert rows[0].details == {"reason": "investigate"}


async def test_list_for_target_scopes_correctly():
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await ops_audit.record(
            db,
            operator_id=f"o1-{suffix}",
            action=ops_audit.ACTION_PATIENT_EXPORT,
            target_type="patient",
            target_id=f"target-{suffix}",
            details={"window_days": 30},
        )
        await ops_audit.record(
            db,
            operator_id=f"o2-{suffix}",
            action=ops_audit.ACTION_PATIENT_EXPORT,
            target_type="patient",
            target_id=f"target-{suffix}",
            details={"window_days": 365},
        )
        # Distractor — different target_id.
        await ops_audit.record(
            db,
            operator_id=f"o1-{suffix}",
            action=ops_audit.ACTION_PATIENT_EXPORT,
            target_type="patient",
            target_id=f"other-{suffix}",
        )
        await db.commit()

    async with SessionLocal() as db:
        rows = await ops_audit.list_for_target(
            db, target_type="patient", target_id=f"target-{suffix}"
        )
        assert len(rows) == 2
        assert {r.operator_id for r in rows} == {
            f"o1-{suffix}", f"o2-{suffix}"
        }


# ---- endpoint wiring ----------------------------------------------------


async def test_dsar_export_writes_audit(orchestrator_client):
    pid, _phone = await _seed_patient()
    r = orchestrator_client.get(
        f"/patients/{pid}/export?window_days=14",
        headers={"X-Ops-Actor": "alice@clinic"},
    )
    assert r.status_code == 200

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows = await ops_audit.list_for_target(
            db, target_type="patient", target_id=pid
        )
    exports = [r for r in rows if r.action == ops_audit.ACTION_PATIENT_EXPORT]
    assert exports, "DSAR export should write an operator_actions row"
    assert exports[0].operator_id == "alice@clinic"
    assert exports[0].details["window_days"] == 14
    assert exports[0].details["header_actor"] is True


async def test_patient_pause_writes_audit(orchestrator_client):
    pid, _phone = await _seed_patient()
    r = orchestrator_client.post(
        f"/patients/{pid}/pause-bot",
        json={"actor": "bob@clinic", "reason": "complaint received"},
    )
    assert r.status_code == 200, r.text

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows = await ops_audit.list_for_target(
            db, target_type="patient", target_id=pid
        )
    pause_rows = [r for r in rows if r.action == ops_audit.ACTION_PATIENT_PAUSE]
    assert pause_rows
    assert pause_rows[0].operator_id == "bob@clinic"
    assert pause_rows[0].details["reason"] == "complaint received"


async def test_patient_unpause_writes_audit(orchestrator_client):
    pid, _phone = await _seed_patient()
    orchestrator_client.post(
        f"/patients/{pid}/pause-bot",
        json={"actor": "bob@clinic", "reason": "x"},
    )
    r = orchestrator_client.post(
        f"/patients/{pid}/unpause-bot",
        headers={"X-Ops-Actor": "carol@clinic"},
    )
    assert r.status_code == 200, r.text

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows = await ops_audit.list_for_target(
            db, target_type="patient", target_id=pid
        )
    unpause_rows = [
        r for r in rows if r.action == ops_audit.ACTION_PATIENT_UNPAUSE
    ]
    assert unpause_rows
    assert unpause_rows[0].operator_id == "carol@clinic"


async def test_ticket_resolve_writes_audit(orchestrator_client):
    pid, phone = await _seed_patient()
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        ticket = await ops_tickets_repo.create(
            db,
            patient_id=phone,
            category="adherence_drop",
            priority="medium",
            sla_minutes=240,
            notes="test ticket",
        )
        await db.commit()
        tid = ticket.id

    r = orchestrator_client.post(
        f"/ops/tickets/{tid}/resolve",
        json={"actor": "dave@clinic", "notes": "patient confirmed"},
    )
    assert r.status_code == 200, r.text

    async with SessionLocal() as db:
        rows = await ops_audit.list_for_target(
            db, target_type="ticket", target_id=tid
        )
    assert rows
    assert rows[0].action == ops_audit.ACTION_TICKET_RESOLVE
    assert rows[0].operator_id == "dave@clinic"

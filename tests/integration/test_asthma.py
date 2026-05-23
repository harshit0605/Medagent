"""Integration tests for the asthma cohort pack (DB-backed).

Covers rescue-inhaler logging + poor-control ticketing (idempotent), the
trigger diary, the /route end-to-end path (with safety deferral), the new
first-class cohort flags via broadcast targeting, and the pregnancy-cohort
flag sync wired through the pregnancy endpoints.

Skipped when DATABASE_URL is unset.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import OpsTicket, Patient
from app.db.repositories import asthma_triggers as triggers_repo
from app.db.repositories import care_plan_goals as goals_repo
from app.db.repositories import patients as patients_repo
from app.db.session import get_sessionmaker
from services.orchestrator import asthma_handler
from services.orchestrator.broadcast_service import resolve_recipients

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping asthma integration tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


async def _seed_patient(*, cohort_asthma: bool = True) -> tuple[int, str]:
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(
            full_name=f"Asthma Test {suffix}",
            phone=f"asthma-{suffix}",
            consent_sms=True,
            cohort_asthma=cohort_asthma,
        )
        db.add(p)
        await db.flush()
        await db.commit()
        return p.id, p.phone


async def _rescue_observations(patient_id: int) -> list:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        return await goals_repo.list_observations_for_patient(
            db, patient_id, metric_key=asthma_handler.RESCUE_METRIC_KEY
        )


async def _open_control_tickets(phone: str) -> list[OpsTicket]:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        stmt = (
            select(OpsTicket)
            .where(OpsTicket.patient_id == phone)
            .where(OpsTicket.category == asthma_handler.ASTHMA_CONTROL_CATEGORY)
        )
        return list((await db.execute(stmt)).scalars().all())


# ---- rescue-inhaler logging ------------------------------------------------


async def test_handle_rescue_log_persists_observation():
    pid, phone = await _seed_patient()
    delta = await asthma_handler.handle_rescue_log(
        patient_phone=phone, new_user_text="used my reliever 2 times"
    )
    assert delta is not None
    assert "asthma_rescue_log" in delta["audit_reasons"]
    rows = await _rescue_observations(pid)
    assert len(rows) == 1
    assert int(rows[0].value) == 2
    assert rows[0].source == "patient_self_report"


async def test_rescue_poor_control_opens_ticket_idempotently():
    pid, phone = await _seed_patient()
    # One big report trips the 7-day puff threshold (>= 8).
    delta = await asthma_handler.handle_rescue_log(
        patient_phone=phone, new_user_text="used 8 puffs of ventolin"
    )
    assert delta is not None
    assert "asthma_poor_control" in delta["audit_reasons"]
    tickets = await _open_control_tickets(phone)
    assert len(tickets) == 1

    # A second poor-control report must NOT open a duplicate open ticket.
    await asthma_handler.handle_rescue_log(
        patient_phone=phone, new_user_text="2 more puffs ventolin"
    )
    tickets_after = await _open_control_tickets(phone)
    assert len(tickets_after) == 1


async def test_handle_rescue_log_unknown_patient_returns_none():
    delta = await asthma_handler.handle_rescue_log(
        patient_phone="asthma-nobody-xyz", new_user_text="used my reliever twice"
    )
    assert delta is None


# ---- trigger diary ---------------------------------------------------------


async def test_handle_trigger_log_persists():
    pid, phone = await _seed_patient()
    delta = await asthma_handler.handle_trigger_log(
        patient_phone=phone, new_user_text="triggered by dust"
    )
    assert delta is not None
    assert delta["audit_reasons"] == ["asthma_trigger_log"]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows = await triggers_repo.list_for_patient(db, pid)
    assert len(rows) == 1
    assert rows[0].trigger_text == "dust"


# ---- /route end-to-end -----------------------------------------------------


async def test_route_asthma_rescue_end_to_end(orchestrator_client):
    pid, phone = await _seed_patient()
    resp = orchestrator_client.post(
        "/route",
        json={
            "message": {
                "patient_id": phone,
                "text": "used my reliever 4 times",
                "message_id": f"wamid-{uuid.uuid4().hex[:8]}",
            },
        },
    )
    assert resp.status_code == 200
    rows = await _rescue_observations(pid)
    assert len(rows) == 1
    assert int(rows[0].value) == 4


async def test_route_asthma_plus_symptom_defers_to_safety(orchestrator_client):
    """A rescue report bundled with an adverse-event signal must route to
    clinical triage first — no silent asthma log."""
    pid, phone = await _seed_patient()
    resp = orchestrator_client.post(
        "/route",
        json={
            "message": {
                "patient_id": phone,
                "text": "used my reliever 5 times, I have a side effect feeling breathless",
                "message_id": f"wamid-{uuid.uuid4().hex[:8]}",
            },
        },
    )
    assert resp.status_code == 200
    rows = await _rescue_observations(pid)
    assert rows == []  # safety path took over


# ---- first-class cohort flags ----------------------------------------------


async def test_asthma_cohort_broadcast_targeting():
    pid, _phone = await _seed_patient(cohort_asthma=True)
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        recipients = await resolve_recipients(
            db, cohort_filter={"cohort": "asthma"}
        )
    assert any(p.id == pid for p in recipients)


async def test_pregnancy_endpoints_sync_cohort_flag(orchestrator_client):
    pid, _phone = await _seed_patient(cohort_asthma=False)
    lmp = datetime.now(timezone.utc).date() - timedelta(weeks=10)
    created = orchestrator_client.post(
        f"/patients/{pid}/pregnancy", json={"lmp_date": lmp.isoformat()}
    )
    assert created.status_code == 200

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        patient = await patients_repo.get(db, pid)
        assert patient.cohort_pregnancy is True

    preg_id = created.json()["id"]
    ended = orchestrator_client.post(
        f"/patients/{pid}/pregnancy/{preg_id}/end", json={}
    )
    assert ended.status_code == 200
    async with SessionLocal() as db:
        patient = await patients_repo.get(db, pid)
        assert patient.cohort_pregnancy is False

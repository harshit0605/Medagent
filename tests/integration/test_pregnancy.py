"""Integration tests for the pregnancy timeline engine (DB-backed).

Covers the intake/management endpoints (create → eager materialize, get,
duplicate guard, end → cancel), and the dispatcher builders that render the
queued reminders into WhatsApp sends (template out-of-CSW, freeform in-CSW,
ReminderNotApplicable once the pregnancy ends).

Skipped when DATABASE_URL is unset.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Patient, ScheduledEvent, ScheduledEventStatus
from app.db.repositories import patient_inbound as patient_inbound_repo
from app.db.repositories import pregnancies as pregnancies_repo
from app.db.session import get_sessionmaker
from services.scheduler import dispatcher
from services.scheduler import pregnancy_milestones as pm

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping pregnancy integration tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


async def _seed_patient(name: str = "Asha Test") -> tuple[int, str]:
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(
            full_name=f"{name} {suffix}",
            phone=f"preg-{suffix}",
            consent_sms=True,
        )
        db.add(p)
        await db.flush()
        await db.commit()
        return p.id, p.phone


async def _pending_pregnancy_events(phone: str) -> list[ScheduledEvent]:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        stmt = (
            select(ScheduledEvent)
            .where(ScheduledEvent.patient_id == phone)
            .where(ScheduledEvent.event_type.in_(pm._OUR_EVENT_TYPES))
        )
        return list((await db.execute(stmt)).scalars().all())


# A LMP ~10 weeks ago guarantees plenty of *future* milestones + check-ins
# regardless of the actual test run date.
def _lmp_10w_ago() -> date:
    return datetime.now(timezone.utc).date() - timedelta(weeks=10)


# ---- endpoints -------------------------------------------------------------


async def test_create_pregnancy_materializes_and_summarizes(orchestrator_client):
    pid, phone = await _seed_patient()
    lmp = _lmp_10w_ago()
    resp = orchestrator_client.post(
        f"/patients/{pid}/pregnancy",
        json={"lmp_date": lmp.isoformat()},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # EDD derived (LMP + 280 days), GA ~10 weeks, second trimester soon.
    assert body["edd"] == (lmp + timedelta(days=280)).isoformat()
    assert body["gestational_week"] in (9, 10, 11)
    assert body["status"] == "active"
    assert body["next_milestone"] is not None

    # Eager materialize queued both kinds of reminders.
    events = await _pending_pregnancy_events(phone)
    types = {e.event_type for e in events}
    assert pm.PREGNANCY_MILESTONE_EVENT_TYPE in types
    assert pm.PREGNANCY_WEEKLY_EVENT_TYPE in types
    assert all(e.status == ScheduledEventStatus.pending for e in events)


async def test_get_active_pregnancy(orchestrator_client):
    pid, _phone = await _seed_patient()
    lmp = _lmp_10w_ago()
    orchestrator_client.post(
        f"/patients/{pid}/pregnancy", json={"lmp_date": lmp.isoformat()}
    )

    resp = orchestrator_client.get(f"/patients/{pid}/pregnancy")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["patient_id"] == pid
    assert body["status"] == "active"
    assert body["lmp_date"] == lmp.isoformat()


async def test_create_duplicate_active_returns_409(orchestrator_client):
    pid, _phone = await _seed_patient()
    lmp = _lmp_10w_ago()
    first = orchestrator_client.post(
        f"/patients/{pid}/pregnancy", json={"lmp_date": lmp.isoformat()}
    )
    assert first.status_code == 200
    second = orchestrator_client.post(
        f"/patients/{pid}/pregnancy", json={"lmp_date": lmp.isoformat()}
    )
    assert second.status_code == 409


async def test_get_active_pregnancy_404_when_none(orchestrator_client):
    pid, _phone = await _seed_patient()
    resp = orchestrator_client.get(f"/patients/{pid}/pregnancy")
    assert resp.status_code == 404


async def test_create_requires_lmp_or_edd(orchestrator_client):
    pid, _phone = await _seed_patient()
    resp = orchestrator_client.post(f"/patients/{pid}/pregnancy", json={})
    assert resp.status_code == 400


async def test_end_pregnancy_cancels_pending_reminders(orchestrator_client):
    pid, phone = await _seed_patient()
    lmp = _lmp_10w_ago()
    created = orchestrator_client.post(
        f"/patients/{pid}/pregnancy", json={"lmp_date": lmp.isoformat()}
    ).json()
    preg_id = created["id"]

    before = await _pending_pregnancy_events(phone)
    assert before, "expected materialized reminders before ending"

    resp = orchestrator_client.post(
        f"/patients/{pid}/pregnancy/{preg_id}/end",
        json={"reason": "delivered"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ended"

    after = await _pending_pregnancy_events(phone)
    # No pending pregnancy events remain — all were skipped.
    assert all(e.status == ScheduledEventStatus.skipped for e in after)


# ---- dispatcher builders ---------------------------------------------------


def _weekly_event(*, phone: str, pregnancy_id: int, patient_db_id: int):
    return SimpleNamespace(
        id=9001,
        event_type=pm.PREGNANCY_WEEKLY_EVENT_TYPE,
        patient_id=phone,
        payload={
            "pregnancy_id": pregnancy_id,
            "patient_db_id": patient_db_id,
            "ga_week": 20,
            "focus": "Your anomaly scan is due around now.",
            "target_date_iso": "2026-06-01",
        },
    )


async def test_build_weekly_reminder_uses_template_out_of_csw():
    pid, phone = await _seed_patient(name="Meera")
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        preg = await pregnancies_repo.create(
            db, patient_id=pid, lmp_date=_lmp_10w_ago()
        )
        await db.commit()
        preg_id = preg.id

    event = _weekly_event(phone=phone, pregnancy_id=preg_id, patient_db_id=pid)
    async with SessionLocal() as db:
        # No prior inbound → patient is OUT of the customer-service window.
        msg = await dispatcher._build_pregnancy_weekly_reminder(db, event)

    assert msg["use_template"] is True
    assert msg["template_name"] == "pregnancy_weekly_v1"
    assert msg["template_params"]["2_week"] == "20"
    assert msg["template_params"]["1_name"] == "Meera"
    assert "anomaly scan" in msg["template_params"]["3_focus"]


async def test_build_weekly_reminder_freeform_in_csw():
    pid, phone = await _seed_patient(name="Riya")
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        preg = await pregnancies_repo.create(
            db, patient_id=pid, lmp_date=_lmp_10w_ago()
        )
        # Patient messaged us just now → inside the 24h CSW.
        await patient_inbound_repo.set_last_inbound(
            db, phone, datetime.now(timezone.utc)
        )
        await db.commit()
        preg_id = preg.id

    event = _weekly_event(phone=phone, pregnancy_id=preg_id, patient_db_id=pid)
    async with SessionLocal() as db:
        msg = await dispatcher._build_pregnancy_weekly_reminder(db, event)

    assert msg["use_template"] is False
    assert "week 20" in msg["body"]
    assert "Riya" in msg["body"]


async def test_build_milestone_reminder_skips_ended_pregnancy():
    pid, phone = await _seed_patient()
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        preg = await pregnancies_repo.create(
            db, patient_id=pid, lmp_date=_lmp_10w_ago()
        )
        await db.flush()
        await pregnancies_repo.end_pregnancy(db, preg.id, reason="miscarriage")
        await db.commit()
        preg_id = preg.id

    event = SimpleNamespace(
        id=9002,
        event_type=pm.PREGNANCY_MILESTONE_EVENT_TYPE,
        patient_id=phone,
        payload={
            "pregnancy_id": preg_id,
            "patient_db_id": pid,
            "milestone_key": "scan_anomaly",
            "kind": "scan",
            "title": "Anomaly scan (TIFFA)",
            "detail": "the detailed 18–22 week anatomy ultrasound",
            "ga_week": 20,
            "target_date_iso": "2026-06-01",
        },
    )
    async with SessionLocal() as db:
        with pytest.raises(dispatcher.ReminderNotApplicable):
            await dispatcher._build_pregnancy_milestone_reminder(db, event)

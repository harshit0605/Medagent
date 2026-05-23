"""Integration tests for the post-op checklist + wound-photo queue (DB-backed).

Skipped when DATABASE_URL is unset.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import OpsTicket, Patient, ScheduledEvent
from app.db.repositories import patients as patients_repo
from app.db.repositories import post_op as post_op_repo
from app.db.session import get_sessionmaker
from services.orchestrator import wound_photo_handler
from services.scheduler import dispatcher

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping post-op tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


async def _seed_patient() -> tuple[int, str]:
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(
            full_name=f"PostOp {suffix}",
            phone=f"postop-{suffix}",
            consent_sms=True,
        )
        db.add(p)
        await db.flush()
        await db.commit()
        return p.id, p.phone


async def _post_op_events(phone: str) -> list[ScheduledEvent]:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        stmt = (
            select(ScheduledEvent)
            .where(ScheduledEvent.patient_id == phone)
            .where(ScheduledEvent.event_type == "post_op_check_due")
        )
        return list((await db.execute(stmt)).scalars().all())


# ---- endpoints -------------------------------------------------------------


async def test_create_post_op_materializes_and_sets_flag(orchestrator_client):
    pid, phone = await _seed_patient()
    today = datetime.now(timezone.utc).date()
    resp = orchestrator_client.post(
        f"/patients/{pid}/post-op",
        json={"procedure_name": "Appendectomy", "surgery_date": today.isoformat()},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "active"
    assert body["next_check"] is not None

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        patient = await patients_repo.get(db, pid)
    assert patient.cohort_post_op is True

    events = await _post_op_events(phone)
    assert events  # checklist materialized


async def test_create_duplicate_409(orchestrator_client):
    pid, _phone = await _seed_patient()
    today = datetime.now(timezone.utc).date().isoformat()
    first = orchestrator_client.post(
        f"/patients/{pid}/post-op",
        json={"procedure_name": "Hernia repair", "surgery_date": today},
    )
    assert first.status_code == 200
    dup = orchestrator_client.post(
        f"/patients/{pid}/post-op",
        json={"procedure_name": "Hernia repair", "surgery_date": today},
    )
    assert dup.status_code == 409


async def test_end_cancels_and_clears_flag(orchestrator_client):
    pid, phone = await _seed_patient()
    today = datetime.now(timezone.utc).date().isoformat()
    created = orchestrator_client.post(
        f"/patients/{pid}/post-op",
        json={"procedure_name": "Knee surgery", "surgery_date": today},
    ).json()
    assert await _post_op_events(phone)  # some pending

    resp = orchestrator_client.post(
        f"/patients/{pid}/post-op/{created['id']}/end",
        json={"reason": "recovered"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ended"

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        patient = await patients_repo.get(db, pid)
        from app.db.models import ScheduledEventStatus

        stmt = (
            select(ScheduledEvent)
            .where(ScheduledEvent.patient_id == phone)
            .where(ScheduledEvent.event_type == "post_op_check_due")
            .where(ScheduledEvent.status == ScheduledEventStatus.pending)
        )
        still_pending = list((await db.execute(stmt)).scalars().all())
    assert patient.cohort_post_op is False
    assert still_pending == []


# ---- wound photo → review queue --------------------------------------------


async def test_wound_photo_opens_review_ticket():
    pid, phone = await _seed_patient()
    delta = await wound_photo_handler.handle_wound_photo(
        patient_phone=phone,
        new_user_text="[wound-photo] public_path=/uploads/wounds/x.jpg mime=image/jpeg",
    )
    assert delta is not None
    assert "wound_photo_received" in delta["audit_reasons"]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        stmt = (
            select(OpsTicket)
            .where(OpsTicket.patient_id == phone)
            .where(OpsTicket.category == "wound_review")
        )
        tickets = list((await db.execute(stmt)).scalars().all())
    assert len(tickets) == 1
    assert "/uploads/wounds/x.jpg" in (tickets[0].notes or "")


async def test_route_wound_photo_end_to_end(orchestrator_client):
    pid, phone = await _seed_patient()
    resp = orchestrator_client.post(
        "/route",
        json={
            "message": {
                "patient_id": phone,
                "text": "[wound-photo] public_path=/uploads/wounds/y.jpg",
                "message_id": f"wamid-{uuid.uuid4().hex[:8]}",
            },
        },
    )
    assert resp.status_code == 200
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        stmt = (
            select(OpsTicket)
            .where(OpsTicket.patient_id == phone)
            .where(OpsTicket.category == "wound_review")
        )
        tickets = list((await db.execute(stmt)).scalars().all())
    assert len(tickets) == 1


# ---- dispatcher render -----------------------------------------------------


async def _make_episode(pid: int) -> int:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        ep = await post_op_repo.create(
            db,
            patient_id=pid,
            procedure_name="Appendectomy",
            surgery_date=date(2026, 1, 1),
        )
        await db.commit()
        return ep.id


async def test_dispatcher_builds_post_op_check_template():
    pid, phone = await _seed_patient()
    ep_id = await _make_episode(pid)
    event = SimpleNamespace(
        id=8201,
        event_type="post_op_check_due",
        patient_id=phone,
        payload={
            "episode_id": ep_id,
            "post_op_day": 3,
            "check_key": "day3_wound",
            "title": "Wound check",
            "detail": "signs of infection",
            "procedure_name": "Appendectomy",
        },
    )
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        msg = await dispatcher._build_post_op_check(db, event)
    assert msg["use_template"] is True
    assert msg["template_name"] == "post_op_check_v1"
    assert msg["template_params"]["1_day"] == "3"


async def test_dispatcher_post_op_wound_photo_variant_asks_for_photo():
    pid, phone = await _seed_patient()
    ep_id = await _make_episode(pid)
    event = SimpleNamespace(
        id=8202,
        event_type="post_op_check_due",
        patient_id=phone,
        payload={
            "episode_id": ep_id,
            "post_op_day": 2,
            "check_key": "wound_photo",
            "title": "Wound photo",
            "detail": "a photo of your wound",
            "procedure_name": "Appendectomy",
        },
    )
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        msg = await dispatcher._build_post_op_check(db, event)
    assert "reply with" in msg["body"].lower()

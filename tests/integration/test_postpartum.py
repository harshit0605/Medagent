"""Integration tests for the postpartum extension to the pregnancy engine
(DB-backed).

Covers:
- End-pregnancy with ``birth_outcome=delivered`` + ``delivery_date`` →
  auto-transitions the row into postpartum + eagerly materializes the first
  PP reminders.
- End-pregnancy with non-delivered outcomes (miscarriage / stillbirth) does
  NOT start a postpartum phase.
- GET /patients/{id}/postpartum returns the active PP, 404 when none.
- POST /patients/{id}/postpartum/{pid}/end closes the PP phase + cancels
  pending PP reminders.
- Dispatcher renders ``postpartum_check_v1`` template out-of-CSW, freeform
  in-CSW, and raises ``ReminderNotApplicable`` once the PP phase ends.

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
from services.scheduler import postpartum_milestones as ppm

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping postpartum integration tests",
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
            phone=f"pp-{suffix}",
            consent_sms=True,
        )
        db.add(p)
        await db.flush()
        await db.commit()
        return p.id, p.phone


async def _pending_pp_events(phone: str) -> list[ScheduledEvent]:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        stmt = (
            select(ScheduledEvent)
            .where(ScheduledEvent.patient_id == phone)
            .where(ScheduledEvent.event_type.in_(ppm._OUR_EVENT_TYPES))
        )
        return list((await db.execute(stmt)).scalars().all())


def _lmp_full_term_ago() -> date:
    """LMP 41 weeks ago so an immediate delivery + EDD-passed are plausible."""
    return datetime.now(timezone.utc).date() - timedelta(weeks=41)


# ---- end-pregnancy → postpartum transition ---------------------------------


async def test_end_pregnancy_delivered_with_date_starts_postpartum(
    orchestrator_client,
):
    pid, phone = await _seed_patient()
    lmp = _lmp_full_term_ago()
    created = orchestrator_client.post(
        f"/patients/{pid}/pregnancy", json={"lmp_date": lmp.isoformat()}
    ).json()
    preg_id = created["id"]

    today = datetime.now(timezone.utc).date()
    resp = orchestrator_client.post(
        f"/patients/{pid}/pregnancy/{preg_id}/end",
        json={
            "reason": "vaginal delivery",
            "birth_outcome": "delivered",
            "delivery_date": today.isoformat(),
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ended"

    # GET active postpartum returns the same row, now in PP phase.
    pp_resp = orchestrator_client.get(f"/patients/{pid}/postpartum")
    assert pp_resp.status_code == 200, pp_resp.text
    pp_body = pp_resp.json()
    assert pp_body["pregnancy_id"] == preg_id
    assert pp_body["postpartum_active"] is True
    assert pp_body["birth_outcome"] == "delivered"
    assert pp_body["delivery_date"] == today.isoformat()
    assert pp_body["pp_week"] == 0
    # Next milestone should be the day-2 early PP check.
    assert pp_body["next_milestone"] is not None
    assert pp_body["next_milestone"]["key"] == "pp_visit_early"

    # Eager materialize queued both kinds of PP reminders.
    events = await _pending_pp_events(phone)
    types = {e.event_type for e in events}
    assert ppm.POSTPARTUM_MILESTONE_EVENT_TYPE in types
    assert ppm.POSTPARTUM_WEEKLY_EVENT_TYPE in types
    assert all(e.status == ScheduledEventStatus.pending for e in events)


async def test_end_pregnancy_miscarriage_does_not_start_postpartum(
    orchestrator_client,
):
    pid, phone = await _seed_patient()
    lmp = _lmp_full_term_ago()
    created = orchestrator_client.post(
        f"/patients/{pid}/pregnancy", json={"lmp_date": lmp.isoformat()}
    ).json()
    preg_id = created["id"]

    resp = orchestrator_client.post(
        f"/patients/{pid}/pregnancy/{preg_id}/end",
        json={
            "reason": "early miscarriage",
            "birth_outcome": "miscarriage",
            # delivery_date intentionally omitted — caller has no birth date
        },
    )
    assert resp.status_code == 200

    # No PP — endpoint returns 404.
    pp_resp = orchestrator_client.get(f"/patients/{pid}/postpartum")
    assert pp_resp.status_code == 404

    # And no PP events were materialized for this phone.
    events = await _pending_pp_events(phone)
    assert events == []


async def test_end_pregnancy_delivered_without_date_does_not_start_postpartum(
    orchestrator_client,
):
    """Operator forgot the delivery_date: we don't guess. Episode just
    closes; operator can backfill via a follow-up endpoint later (deferred)."""
    pid, phone = await _seed_patient()
    lmp = _lmp_full_term_ago()
    created = orchestrator_client.post(
        f"/patients/{pid}/pregnancy", json={"lmp_date": lmp.isoformat()}
    ).json()
    preg_id = created["id"]

    resp = orchestrator_client.post(
        f"/patients/{pid}/pregnancy/{preg_id}/end",
        json={"reason": "delivered, awaiting details", "birth_outcome": "delivered"},
    )
    assert resp.status_code == 200

    pp_resp = orchestrator_client.get(f"/patients/{pid}/postpartum")
    assert pp_resp.status_code == 404
    assert await _pending_pp_events(phone) == []


async def test_end_postpartum_cancels_pending_reminders(orchestrator_client):
    pid, phone = await _seed_patient()
    lmp = _lmp_full_term_ago()
    preg_id = orchestrator_client.post(
        f"/patients/{pid}/pregnancy", json={"lmp_date": lmp.isoformat()}
    ).json()["id"]
    today = datetime.now(timezone.utc).date()
    orchestrator_client.post(
        f"/patients/{pid}/pregnancy/{preg_id}/end",
        json={
            "reason": "delivered",
            "birth_outcome": "delivered",
            "delivery_date": today.isoformat(),
        },
    )

    before = await _pending_pp_events(phone)
    assert before, "expected materialized PP reminders before ending"

    resp = orchestrator_client.post(
        f"/patients/{pid}/postpartum/{preg_id}/end",
        json={"reason": "transferred to specialist"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["postpartum_active"] is False

    after = await _pending_pp_events(phone)
    assert all(e.status == ScheduledEventStatus.skipped for e in after)

    # GET active is now 404.
    assert orchestrator_client.get(f"/patients/{pid}/postpartum").status_code == 404


async def test_get_active_postpartum_404_when_none(orchestrator_client):
    pid, _phone = await _seed_patient()
    assert orchestrator_client.get(f"/patients/{pid}/postpartum").status_code == 404


# ---- dispatcher builders ---------------------------------------------------


async def _seed_pp_row(name: str = "Meera") -> tuple[int, str, int]:
    """Create patient + pregnancy + transition to PP. Returns (patient_id,
    phone, pregnancy_id)."""
    pid, phone = await _seed_patient(name=name)
    SessionLocal = get_sessionmaker()
    today = datetime.now(timezone.utc).date()
    async with SessionLocal() as db:
        preg = await pregnancies_repo.create(
            db, patient_id=pid, lmp_date=_lmp_full_term_ago()
        )
        await db.flush()
        await pregnancies_repo.end_pregnancy(
            db, preg.id,
            reason="delivered",
            birth_outcome="delivered",
            delivery_date=today,
        )
        await pregnancies_repo.start_postpartum(
            db, preg.id, delivery_date=today
        )
        await db.commit()
        return pid, phone, preg.id


def _weekly_pp_event(*, phone: str, pregnancy_id: int, patient_db_id: int):
    return SimpleNamespace(
        id=9101,
        event_type=ppm.POSTPARTUM_WEEKLY_EVENT_TYPE,
        patient_id=phone,
        payload={
            "pregnancy_id": pregnancy_id,
            "patient_db_id": patient_db_id,
            "pp_week": 2,
            "focus": "First few weeks — feeding rhythm and watch for infection.",
            "target_date_iso": "2026-06-01",
        },
    )


async def test_build_postpartum_weekly_uses_template_out_of_csw():
    pid, phone, preg_id = await _seed_pp_row(name="Meera")
    event = _weekly_pp_event(phone=phone, pregnancy_id=preg_id, patient_db_id=pid)
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        # No prior inbound → OUT of CSW.
        msg = await dispatcher._build_postpartum_weekly_reminder(db, event)
    assert msg["use_template"] is True
    assert msg["template_name"] == "postpartum_check_v1"
    assert msg["template_params"]["2_week"] == "2"
    assert msg["template_params"]["1_name"] == "Meera"
    assert "feeding" in msg["template_params"]["3_focus"]


async def test_build_postpartum_weekly_freeform_in_csw():
    pid, phone, preg_id = await _seed_pp_row(name="Riya")
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await patient_inbound_repo.set_last_inbound(
            db, phone, datetime.now(timezone.utc)
        )
        await db.commit()
    event = _weekly_pp_event(phone=phone, pregnancy_id=preg_id, patient_db_id=pid)
    async with SessionLocal() as db:
        msg = await dispatcher._build_postpartum_weekly_reminder(db, event)
    assert msg["use_template"] is False
    assert "week" in msg["body"]
    assert "Riya" in msg["body"]


async def test_build_postpartum_milestone_skips_after_pp_ends():
    pid, phone, preg_id = await _seed_pp_row()
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await pregnancies_repo.end_postpartum(db, preg_id, reason="closed")
        await db.commit()

    event = SimpleNamespace(
        id=9102,
        event_type=ppm.POSTPARTUM_MILESTONE_EVENT_TYPE,
        patient_id=phone,
        payload={
            "pregnancy_id": preg_id,
            "patient_db_id": pid,
            "milestone_key": "pp_visit_6w",
            "kind": "visit",
            "title": "6-week postnatal visit",
            "detail": "your 6-week postnatal visit + contraception conversation.",
            "pp_day": 42,
            "target_date_iso": "2026-06-15",
        },
    )
    async with SessionLocal() as db:
        with pytest.raises(dispatcher.ReminderNotApplicable):
            await dispatcher._build_postpartum_milestone_reminder(db, event)


async def test_build_postpartum_milestone_renders_day_phrase_for_early():
    """Days <7 render as 'day N' (not 'N week(s)') in the body copy."""
    pid, phone, preg_id = await _seed_pp_row(name="Tara")
    event = SimpleNamespace(
        id=9103,
        event_type=ppm.POSTPARTUM_MILESTONE_EVENT_TYPE,
        patient_id=phone,
        payload={
            "pregnancy_id": preg_id,
            "patient_db_id": pid,
            "milestone_key": "pp_visit_early",
            "kind": "visit",
            "title": "Early postpartum check",
            "detail": "an early check-in.",
            "pp_day": 2,
            "target_date_iso": "2026-06-01",
        },
    )
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        msg = await dispatcher._build_postpartum_milestone_reminder(db, event)
    # Either body (in-CSW) or template params (out-of-CSW) carries "day 2".
    rendered = msg.get("body") or msg.get("template_params", {}).get("3_focus", "")
    assert "day 2" in (rendered + msg.get("body", "")).lower()

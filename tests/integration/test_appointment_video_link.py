"""Telehealth video link on appointments (I6)."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.db.models import (
    Appointment,
    AppointmentStatus,
    Doctor,
    DoctorOAuthStatus,
    Patient,
)
from app.db.session import get_sessionmaker
from services.scheduler import dispatcher

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping integration tests",
)


@pytest.fixture(scope="module")
def client():
    from services.orchestrator.main import app

    with TestClient(app) as c:
        yield c


async def _seed_appointment() -> tuple[int, str, int]:
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(full_name=f"Tele {suffix}", phone=f"tele-{suffix}", consent_sms=True)
        db.add(p)
        d = Doctor(
            name=f"Dr Tele {suffix}",
            email=f"tele-{suffix}@x.test",
            oauth_status=DoctorOAuthStatus.disconnected,
        )
        db.add(d)
        await db.flush()
        start = datetime.now(timezone.utc) + timedelta(days=1)
        appt = Appointment(
            patient_id=p.id,
            doctor_id=d.id,
            scheduled_for=start,
            end_at=start + timedelta(minutes=30),
            status=AppointmentStatus.confirmed,
        )
        db.add(appt)
        await db.flush()
        await db.commit()
        return appt.id, p.phone, p.id


def test_set_video_link_endpoint(client):
    import asyncio

    appt_id, _phone, _pid = asyncio.get_event_loop().run_until_complete(
        _seed_appointment()
    )
    r = client.post(
        f"/appointments/{appt_id}/video-link",
        json={"video_link": "https://meet.example/abc-defg"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["video_link"] == "https://meet.example/abc-defg"

    # Clearing it.
    r2 = client.post(f"/appointments/{appt_id}/video-link", json={"video_link": ""})
    assert r2.status_code == 200
    assert r2.json()["video_link"] is None


def test_set_video_link_unknown_404(client):
    r = client.post("/appointments/99999999/video-link", json={"video_link": "x"})
    assert r.status_code == 404


async def test_reminder_includes_join_line_when_link_set():
    appt_id, phone, pid = await _seed_appointment()
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        appt = await db.get(Appointment, appt_id)
        appt.video_link = "https://meet.example/xyz-1234"
        await db.commit()
        doctor = await db.get(Doctor, appt.doctor_id)

    # Build the reminder via the dispatcher builder (out-of-CSW → but the
    # join line is added to the body regardless of mode).
    event = SimpleNamespace(
        id=7001,
        event_type="appointment_reminder_24h",
        patient_id=phone,
        payload={
            "appointment_id": appt_id,
            "doctor_id": doctor.id,
            "appointment_start_iso": appt.scheduled_for.isoformat(),
            "timezone": "Asia/Kolkata",
        },
    )
    async with SessionLocal() as db:
        msg = await dispatcher._build_appointment_reminder(db, event)
    assert "Join here: https://meet.example/xyz-1234" in msg["body"]

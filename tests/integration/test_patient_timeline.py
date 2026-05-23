"""Integration tests for the unified per-patient timeline.

End-to-end against real Postgres because:
    1. Five different source tables with mismatched keying
       conventions (string phone vs int FK) — correctness here
       depends on the live join graph.
    2. The endpoint's ``since/until`` filter pushes timestamp
       comparisons into Postgres; behavior under TZ-naive vs
       TZ-aware is dialect-sensitive.
    3. Lab-followup events emit up to 3 timeline rows from a
       single source row (booked / completed / reviewed), and
       getting that right is exactly the kind of fan-out we
       can't unit-test cleanly.

Skipped when DATABASE_URL is unset.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.db.models import (
    AdherenceEvent,
    AdherenceStatus,
    AppointmentStatus,
    FollowupStatus,
    Patient,
)
from app.db.repositories import (
    appointments as appointments_repo,
    doctors as doctors_repo,
    lab_followups as lab_followups_repo,
    message_log as message_log_repo,
    ops_tickets as ops_tickets_repo,
    regimens as regimens_repo,
)
from app.db.session import get_sessionmaker
from services.orchestrator import patient_timeline

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping timeline tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


# ---- helpers ---------------------------------------------------------------


async def _seed_patient(*, with_phone: bool = True) -> tuple[int, str]:
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(
            full_name=f"Timeline Test {suffix}",
            phone=f"tl-{suffix}" if with_phone else "",
            consent_sms=True,
        )
        db.add(p)
        await db.flush()
        await db.commit()
        return p.id, p.phone


async def _seed_doctor() -> int:
    suffix = uuid.uuid4().hex[:6]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        d = await doctors_repo.create(
            db,
            name=f"Dr. Timeline {suffix}",
            email=f"tl-{suffix}@example.com",
            phone=f"+91-doctl-{suffix}",
        )
        await db.commit()
        return d.id


async def _seed_regimen(patient_id: int, *, drug: str) -> int:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        r = await regimens_repo.create(
            db,
            patient_id=patient_id,
            medication_name=drug,
            dose="1 tablet",
            schedule={
                "type": "times_of_day",
                "times": ["08:00"],
                "timezone": "Asia/Kolkata",
                "frequency": "daily",
            },
            strict_timing=False,
        )
        await db.commit()
        return r.id


async def _seed_dose(
    *,
    patient_id: int,
    regimen_id: int,
    scheduled_at: datetime,
    status: AdherenceStatus,
    confirmed_at: datetime | None = None,
) -> int:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        row = AdherenceEvent(
            patient_id=patient_id,
            regimen_id=regimen_id,
            scheduled_at=scheduled_at,
            status=status,
            confirmed_at=confirmed_at,
        )
        db.add(row)
        await db.flush()
        await db.commit()
        return row.id


async def _seed_inbound_message(
    *, phone: str, body: str, when: datetime
) -> int:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        row = await message_log_repo.append_inbound(
            db,
            patient_id=phone,
            payload={"type": "text", "text": {"body": body}},
            occurred_at=when,
        )
        await db.commit()
        return row.id


async def _seed_outbound_message(
    *, phone: str, body: str, when: datetime
) -> int:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        row = await message_log_repo.append_outbound(
            db,
            patient_id=phone,
            payload_kind="freeform",
            payload={"body": body},
            occurred_at=when,
        )
        await db.commit()
        return row.id


async def _seed_side_effect_ticket(*, phone: str, said: str) -> int:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        row = await ops_tickets_repo.create(
            db,
            patient_id=phone,
            category="side_effect_report",
            priority="high",
            sla_minutes=60,
            notes=f"patient said: {said}",
        )
        await db.commit()
        return row.id


async def _seed_appointment(
    *,
    patient_id: int,
    doctor_id: int,
    scheduled_for: datetime,
    status: AppointmentStatus = AppointmentStatus.confirmed,
) -> int:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        row = await appointments_repo.create_confirmed(
            db,
            patient_id=patient_id,
            doctor_id=doctor_id,
            scheduled_for=scheduled_for,
            end_at=scheduled_for + timedelta(minutes=30),
            calendar_event_id=None,
            calendar_html_link="https://cal.example/x",
        )
        if status != AppointmentStatus.confirmed:
            row.status = status
            await db.flush()
        await db.commit()
        return row.id


async def _seed_lab(
    *,
    patient_id: int,
    booked_at: datetime | None = None,
    completed_at: datetime | None = None,
    reviewed_at: datetime | None = None,
) -> int:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        row = await lab_followups_repo.create(
            db,
            patient_id=patient_id,
            test_name="HbA1c",
            due_by=date.today(),
        )
        if booked_at is not None:
            row.booked_at = booked_at
            row.status = FollowupStatus.booked
        if completed_at is not None:
            row.completed_at = completed_at
            row.status = FollowupStatus.completed
        if reviewed_at is not None:
            row.reviewed_at = reviewed_at
        await db.flush()
        await db.commit()
        return row.id


# ---- service-level tests ---------------------------------------------------


async def test_timeline_aggregates_messages_in_chronological_order():
    """Inbound + outbound messages within window appear,
    sorted newest-first."""
    pid, phone = await _seed_patient()
    now = datetime.now(timezone.utc)

    await _seed_inbound_message(
        phone=phone,
        body="hi doc, I'm feeling dizzy",
        when=now - timedelta(hours=2),
    )
    await _seed_outbound_message(
        phone=phone,
        body="thanks for reaching out — let's check vitals",
        when=now - timedelta(hours=1),
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        events = await patient_timeline.build_timeline(
            db,
            patient_db_id=pid,
            since=now - timedelta(days=1),
            until=now,
        )

    kinds = [e.kind for e in events]
    assert kinds == ["message_outbound", "message_inbound"]
    assert events[1].body == "hi doc, I'm feeling dizzy"
    assert "thanks" in events[0].body


async def test_timeline_filters_by_window():
    """Events outside [since, until] are excluded."""
    pid, phone = await _seed_patient()
    now = datetime.now(timezone.utc)

    # Inside window
    await _seed_inbound_message(
        phone=phone, body="recent", when=now - timedelta(hours=1)
    )
    # Outside window (60 days ago)
    await _seed_inbound_message(
        phone=phone, body="ancient", when=now - timedelta(days=60)
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        events = await patient_timeline.build_timeline(
            db,
            patient_db_id=pid,
            since=now - timedelta(days=30),
            until=now,
        )

    bodies = [e.body for e in events if e.body]
    assert "recent" in bodies
    assert "ancient" not in bodies


async def test_timeline_filters_by_kind():
    """``kinds`` parameter restricts the output to listed kinds."""
    pid, phone = await _seed_patient()
    regimen_id = await _seed_regimen(pid, drug="Metformin")
    now = datetime.now(timezone.utc)

    await _seed_inbound_message(
        phone=phone, body="hi", when=now - timedelta(hours=1)
    )
    await _seed_dose(
        patient_id=pid,
        regimen_id=regimen_id,
        scheduled_at=now - timedelta(hours=2),
        status=AdherenceStatus.taken,
        confirmed_at=now - timedelta(hours=2),
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        events = await patient_timeline.build_timeline(
            db,
            patient_db_id=pid,
            since=now - timedelta(days=1),
            until=now,
            kinds=("dose_taken",),
        )

    assert len(events) == 1
    assert events[0].kind == "dose_taken"
    assert events[0].title == "Took Metformin"


async def test_timeline_dose_uses_confirmed_at_when_present():
    """Patient confirmed at a different time than scheduled —
    the timeline should show when they ACTUALLY took the dose,
    not when it was scheduled."""
    pid, _ = await _seed_patient()
    regimen_id = await _seed_regimen(pid, drug="Aspirin")
    now = datetime.now(timezone.utc)

    scheduled = now - timedelta(hours=4)
    confirmed = now - timedelta(hours=3)
    await _seed_dose(
        patient_id=pid,
        regimen_id=regimen_id,
        scheduled_at=scheduled,
        status=AdherenceStatus.taken,
        confirmed_at=confirmed,
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        events = await patient_timeline.build_timeline(
            db,
            patient_db_id=pid,
            since=now - timedelta(days=1),
            until=now,
            kinds=("dose_taken",),
        )

    assert len(events) == 1
    # Allow microsecond drift from Postgres round-trip.
    diff = abs(
        (events[0].occurred_at - confirmed).total_seconds()
    )
    assert diff < 1.0, f"expected ~confirmed_at, got {events[0].occurred_at}"


async def test_timeline_missed_dose_falls_back_to_scheduled_at():
    """Missed doses have no confirmed_at — their occurrence
    timestamp must be the scheduled time."""
    pid, _ = await _seed_patient()
    regimen_id = await _seed_regimen(pid, drug="Insulin")
    now = datetime.now(timezone.utc)
    scheduled = now - timedelta(hours=5)

    await _seed_dose(
        patient_id=pid,
        regimen_id=regimen_id,
        scheduled_at=scheduled,
        status=AdherenceStatus.missed,
        confirmed_at=None,
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        events = await patient_timeline.build_timeline(
            db,
            patient_db_id=pid,
            since=now - timedelta(days=1),
            until=now,
            kinds=("dose_missed",),
        )

    assert len(events) == 1
    assert events[0].title == "Missed Insulin"
    diff = abs(
        (events[0].occurred_at - scheduled).total_seconds()
    )
    assert diff < 1.0


async def test_timeline_includes_side_effect_tickets():
    """Side-effect ops tickets surface as ``side_effect`` events
    with the patient quote in the body."""
    pid, phone = await _seed_patient()
    now = datetime.now(timezone.utc)

    ticket_id = await _seed_side_effect_ticket(
        phone=phone, said="severe stomach pain after lunch"
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        events = await patient_timeline.build_timeline(
            db,
            patient_db_id=pid,
            since=now - timedelta(days=1),
            until=now + timedelta(hours=1),
            kinds=("side_effect",),
        )

    assert len(events) == 1
    e = events[0]
    assert e.title == "Side effect reported"
    assert "stomach pain" in (e.body or "")
    assert e.detail["ticket_id"] == ticket_id


async def test_timeline_lab_followup_emits_three_events():
    """A single lab row that's been booked, completed, AND
    reviewed within the window emits THREE timeline events —
    one per transition."""
    pid, _ = await _seed_patient()
    now = datetime.now(timezone.utc)

    await _seed_lab(
        patient_id=pid,
        booked_at=now - timedelta(days=3),
        completed_at=now - timedelta(days=2),
        reviewed_at=now - timedelta(days=1),
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        events = await patient_timeline.build_timeline(
            db,
            patient_db_id=pid,
            since=now - timedelta(days=7),
            until=now,
        )

    kinds = [e.kind for e in events]
    assert kinds.count("lab_booked") == 1
    assert kinds.count("lab_completed") == 1
    assert kinds.count("lab_reviewed") == 1
    # Newest-first ordering means reviewed > completed > booked
    lab_only = [e.kind for e in events if e.kind.startswith("lab_")]
    assert lab_only == ["lab_reviewed", "lab_completed", "lab_booked"]


async def test_timeline_includes_appointments():
    """Confirmed / completed / cancelled / no-show appointments
    surface with the right kind. Proposed slots are ignored."""
    pid, _ = await _seed_patient()
    doctor_id = await _seed_doctor()
    now = datetime.now(timezone.utc)

    appt_id = await _seed_appointment(
        patient_id=pid,
        doctor_id=doctor_id,
        scheduled_for=now - timedelta(days=2),
        status=AppointmentStatus.completed,
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        events = await patient_timeline.build_timeline(
            db,
            patient_db_id=pid,
            since=now - timedelta(days=7),
            until=now + timedelta(hours=1),
            kinds=("appointment_completed",),
        )

    assert len(events) == 1
    assert events[0].kind == "appointment_completed"
    assert events[0].detail["appointment_id"] == appt_id
    assert events[0].detail["calendar_html_link"] == "https://cal.example/x"


async def test_timeline_unknown_patient_raises():
    """Building a timeline for a non-existent patient raises
    ValueError so the endpoint can map to 404."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        with pytest.raises(ValueError, match="not found"):
            await patient_timeline.build_timeline(
                db,
                patient_db_id=999_999_999,
                since=datetime.now(timezone.utc) - timedelta(days=1),
                until=datetime.now(timezone.utc),
            )


# ---- endpoint tests --------------------------------------------------------


async def test_endpoint_returns_events_in_default_window(
    orchestrator_client,
):
    """Hitting GET /patients/{id}/timeline without query params
    returns a 30-day default window with seeded events."""
    pid, phone = await _seed_patient()
    now = datetime.now(timezone.utc)
    await _seed_inbound_message(
        phone=phone, body="endpoint test", when=now - timedelta(hours=1)
    )

    response = orchestrator_client.get(f"/patients/{pid}/timeline")
    assert response.status_code == 200
    data = response.json()
    assert data["patient_id"] == pid
    bodies = [e["body"] for e in data["events"] if e.get("body")]
    assert "endpoint test" in bodies


async def test_endpoint_validates_unknown_kind(orchestrator_client):
    """Unknown ``kinds`` values produce 400, not silent drop."""
    pid, _ = await _seed_patient()
    response = orchestrator_client.get(
        f"/patients/{pid}/timeline?kinds=bogus_kind"
    )
    assert response.status_code == 400
    assert "bogus_kind" in response.text


async def test_endpoint_returns_404_for_missing_patient(
    orchestrator_client,
):
    response = orchestrator_client.get(
        "/patients/999999999/timeline"
    )
    assert response.status_code == 404


async def test_endpoint_validates_since_after_until(orchestrator_client):
    """``since > until`` is operator error — return 400."""
    pid, _ = await _seed_patient()
    now = datetime.now(timezone.utc)
    # Pass via ``params=`` so requests URL-encodes the ``+`` in
    # the tz offset; otherwise FastAPI gets a space and 422s
    # before our application-level since/until check runs.
    response = orchestrator_client.get(
        f"/patients/{pid}/timeline",
        params={
            "since": (now + timedelta(hours=1)).isoformat(),
            "until": (now - timedelta(days=2)).isoformat(),
        },
    )
    assert response.status_code == 400


async def test_endpoint_kind_filter_round_trip(orchestrator_client):
    """Filter to ``message_inbound`` only — outbound messages
    in the same window are excluded."""
    pid, phone = await _seed_patient()
    now = datetime.now(timezone.utc)
    await _seed_inbound_message(
        phone=phone, body="inbound A", when=now - timedelta(hours=2)
    )
    await _seed_outbound_message(
        phone=phone, body="outbound B", when=now - timedelta(hours=1)
    )

    response = orchestrator_client.get(
        f"/patients/{pid}/timeline?kinds=message_inbound"
    )
    assert response.status_code == 200
    data = response.json()
    kinds = [e["kind"] for e in data["events"]]
    assert "message_inbound" in kinds
    assert "message_outbound" not in kinds

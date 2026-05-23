"""Integration tests for the doctor daily digest aggregator + endpoint.

End-to-end against real Postgres because the digest:
    1. Joins ``appointments`` + ``patients`` + ``ops_tickets`` +
       ``appointment_recaps`` to assemble the panel view — only
       meaningful against a real planner.
    2. Filters by category enum + JSON containment in places where
       SQLAlchemy lowering varies by dialect.
    3. Time-bounded queries (last-24h, last-90-days) need real
       timestamp comparisons.

Skipped when DATABASE_URL is unset.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.db.models import (
    Appointment,
    AppointmentStatus,
    AppointmentRecap,
    Doctor,
    Patient,
    RecapStatus,
)
from app.db.repositories import ops_tickets as ops_tickets_repo
from app.db.session import get_sessionmaker
from services.orchestrator import doctor_digest as digest_module

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping doctor digest tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


async def _create_doctor() -> int:
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        doc = Doctor(
            name=f"Dr. Digest Test {suffix}",
            email=f"digest-{suffix}@test.local",
            timezone="UTC",
            calendar_id=f"primary-{suffix}",
        )
        db.add(doc)
        await db.flush()
        await db.commit()
        return doc.id


async def _create_patient(*, full_name: str | None = None) -> tuple[int, str]:
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(
            full_name=full_name or f"Digest Patient {suffix}",
            phone=f"digest-{suffix}",
            consent_sms=True,
        )
        db.add(p)
        await db.flush()
        await db.commit()
        return p.id, p.phone


async def _schedule_appointment(
    *,
    doctor_id: int,
    patient_id: int,
    when: datetime,
    duration_minutes: int = 30,
    status: AppointmentStatus = AppointmentStatus.confirmed,
) -> int:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        appt = Appointment(
            patient_id=patient_id,
            doctor_id=doctor_id,
            scheduled_for=when,
            end_at=when + timedelta(minutes=duration_minutes),
            status=status,
            summary="follow-up",
        )
        db.add(appt)
        await db.flush()
        await db.commit()
        return appt.id


# ---- appointments_today section ------------------------------------------


async def test_digest_includes_today_appointments_only():
    """Appointments outside the UTC day boundary must be excluded.
    A doctor scrolling the digest at 9am should see today's
    schedule, not yesterday's."""
    doctor_id = await _create_doctor()
    pid, _ = await _create_patient()

    today_noon = datetime.now(timezone.utc).replace(
        hour=12, minute=0, second=0, microsecond=0
    )
    yesterday_noon = today_noon - timedelta(days=1)

    today_appt = await _schedule_appointment(
        doctor_id=doctor_id, patient_id=pid, when=today_noon
    )
    await _schedule_appointment(
        doctor_id=doctor_id, patient_id=pid, when=yesterday_noon
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        digest = await digest_module.build_digest(
            db,
            doctor_id=doctor_id,
            doctor_name="Test",
            when=today_noon,
        )

    appt_ids = [a.appointment_id for a in digest.appointments_today]
    assert today_appt in appt_ids
    # Yesterday's appointment is NOT in today.
    assert len(appt_ids) == 1


async def test_digest_appointment_pending_recap_flag():
    """An appointment with a draft recap should flag
    ``has_pending_recap=True`` so the doctor sees what needs
    finishing."""
    doctor_id = await _create_doctor()
    pid, _ = await _create_patient()
    today_noon = datetime.now(timezone.utc).replace(
        hour=12, minute=0, second=0, microsecond=0
    )
    appt_id = await _schedule_appointment(
        doctor_id=doctor_id, patient_id=pid, when=today_noon
    )

    # Drop a draft recap.
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        recap = AppointmentRecap(
            appointment_id=appt_id,
            patient_id=pid,
            doctor_id=doctor_id,
            doctor_notes="draft notes",
            structured_payload={},
            status=RecapStatus.draft,
        )
        db.add(recap)
        await db.commit()

        digest = await digest_module.build_digest(
            db,
            doctor_id=doctor_id,
            doctor_name="Test",
            when=today_noon,
        )

    target = next(
        a
        for a in digest.appointments_today
        if a.appointment_id == appt_id
    )
    assert target.has_pending_recap is True


# ---- recap_drafts_pending section ---------------------------------------


async def test_digest_recap_drafts_filtered_by_doctor_and_status():
    """Recap drafts must be scoped to THIS doctor and to draft
    status — sent recaps shouldn't pollute the pending list."""
    doctor_id = await _create_doctor()
    other_doctor_id = await _create_doctor()
    pid, _ = await _create_patient()

    today = datetime.now(timezone.utc).replace(
        hour=12, minute=0, second=0, microsecond=0
    )
    my_appt = await _schedule_appointment(
        doctor_id=doctor_id, patient_id=pid, when=today
    )
    other_appt = await _schedule_appointment(
        doctor_id=other_doctor_id, patient_id=pid, when=today
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        # Draft for ME — should appear.
        my_draft = AppointmentRecap(
            appointment_id=my_appt,
            patient_id=pid,
            doctor_id=doctor_id,
            doctor_notes="my draft",
            structured_payload={},
            status=RecapStatus.draft,
        )
        # Draft for OTHER doctor — must NOT appear.
        other_draft = AppointmentRecap(
            appointment_id=other_appt,
            patient_id=pid,
            doctor_id=other_doctor_id,
            doctor_notes="other doctor's draft",
            structured_payload={},
            status=RecapStatus.draft,
        )
        # Sent recap for ME — must NOT appear (already sent).
        sent_appt = await _schedule_appointment(
            doctor_id=doctor_id, patient_id=pid, when=today
        )
        my_sent = AppointmentRecap(
            appointment_id=sent_appt,
            patient_id=pid,
            doctor_id=doctor_id,
            doctor_notes="my sent",
            structured_payload={},
            status=RecapStatus.sent,
        )
        db.add_all([my_draft, other_draft, my_sent])
        await db.commit()

        digest = await digest_module.build_digest(
            db,
            doctor_id=doctor_id,
            doctor_name="Test",
            when=today,
        )

    appt_ids = {r.appointment_id for r in digest.recap_drafts_pending}
    assert my_appt in appt_ids
    assert other_appt not in appt_ids
    assert sent_appt not in appt_ids


# ---- side_effect_reports_24h section ------------------------------------


async def test_digest_side_effects_filtered_to_panel_and_window():
    """Side-effect reports must be scoped to the doctor's panel
    AND to the last 24h. Yesterday's reports are out; off-panel
    patients' reports are out."""
    doctor_id = await _create_doctor()
    panel_pid, panel_phone = await _create_patient()
    off_panel_pid, off_panel_phone = await _create_patient()

    # Put panel patient on this doctor's calendar (last 30 days).
    await _schedule_appointment(
        doctor_id=doctor_id,
        patient_id=panel_pid,
        when=datetime.now(timezone.utc) - timedelta(days=15),
    )
    # off_panel_pid has no appointment with this doctor → not panel.

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        # Recent panel report — should appear.
        recent_ticket = await ops_tickets_repo.create(
            db,
            patient_id=panel_phone,
            category="side_effect_report",
            priority="high",
            sla_minutes=30,
            notes="Patient said:\n  > new dizziness",
        )
        # Old panel report (>24h) — must NOT appear.
        old_ticket = await ops_tickets_repo.create(
            db,
            patient_id=panel_phone,
            category="side_effect_report",
            priority="high",
            sla_minutes=30,
            notes="Patient said:\n  > old issue",
        )
        old_ticket.created_at = datetime.now(timezone.utc) - timedelta(
            days=2
        )
        # Off-panel recent report — must NOT appear.
        off_panel_ticket = await ops_tickets_repo.create(
            db,
            patient_id=off_panel_phone,
            category="side_effect_report",
            priority="high",
            sla_minutes=30,
            notes="Patient said:\n  > unrelated patient",
        )
        await db.flush()
        await db.commit()

        digest = await digest_module.build_digest(
            db,
            doctor_id=doctor_id,
            doctor_name="Test",
            when=datetime.now(timezone.utc),
        )

    ticket_ids = {s.ticket_id for s in digest.side_effect_reports_24h}
    assert recent_ticket.id in ticket_ids
    assert old_ticket.id not in ticket_ids
    assert off_panel_ticket.id not in ticket_ids


async def test_digest_side_effect_reports_extract_text():
    """``reported_text`` should be extracted from the ticket
    notes via the shared parser so the digest matches the
    side-effect-history view rendering."""
    doctor_id = await _create_doctor()
    panel_pid, panel_phone = await _create_patient()
    await _schedule_appointment(
        doctor_id=doctor_id,
        patient_id=panel_pid,
        when=datetime.now(timezone.utc) - timedelta(days=1),
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await ops_tickets_repo.create(
            db,
            patient_id=panel_phone,
            category="side_effect_report",
            priority="high",
            sla_minutes=30,
            notes="Patient said:\n  > rash from new prescription",
        )
        await db.commit()

        digest = await digest_module.build_digest(
            db,
            doctor_id=doctor_id,
            doctor_name="Test",
            when=datetime.now(timezone.utc),
        )

    target = next(
        s for s in digest.side_effect_reports_24h
        if s.patient_id == panel_pid
    )
    assert target.reported_text == "rash from new prescription"


# ---- open_tickets section -----------------------------------------------


async def test_digest_open_tickets_filters_priority_and_status():
    """Only open/acked + high/critical/p0/p1 tickets count.
    Low-priority + resolved + side-effect-report tickets are
    excluded (the side-effect section has its own bucket)."""
    doctor_id = await _create_doctor()
    pid, phone = await _create_patient()
    await _schedule_appointment(
        doctor_id=doctor_id,
        patient_id=pid,
        when=datetime.now(timezone.utc) - timedelta(days=5),
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        high = await ops_tickets_repo.create(
            db,
            patient_id=phone,
            category="missed_dose",
            priority="high",
            sla_minutes=240,
            notes="missed",
        )
        low = await ops_tickets_repo.create(
            db,
            patient_id=phone,
            category="lab_help",
            priority="p3",
            sla_minutes=240,
            notes="low pri",
        )
        # Side-effect — handled by its own section, must NOT
        # leak into open_tickets.
        await ops_tickets_repo.create(
            db,
            patient_id=phone,
            category="side_effect_report",
            priority="high",
            sla_minutes=30,
            notes="se",
        )
        # Resolved — must NOT appear.
        resolved = await ops_tickets_repo.create(
            db,
            patient_id=phone,
            category="missed_dose",
            priority="high",
            sla_minutes=240,
            notes="closed",
        )
        await ops_tickets_repo.resolve(db, resolved.id, actor="ops")
        await db.commit()

        digest = await digest_module.build_digest(
            db,
            doctor_id=doctor_id,
            doctor_name="Test",
            when=datetime.now(timezone.utc),
        )

    ticket_ids = {t.ticket_id for t in digest.open_tickets}
    assert high.id in ticket_ids
    assert low.id not in ticket_ids
    assert resolved.id not in ticket_ids
    # Side-effect is excluded from open_tickets (lives elsewhere).
    assert all(
        t.category != "side_effect_report" for t in digest.open_tickets
    )


# ---- summary_counts -----------------------------------------------------


async def test_digest_summary_counts_match_section_lengths():
    """Top-line counters MUST match the section lengths exactly —
    a UI rendering "5 appointments today" while the table shows
    only 3 is the kind of bug that erodes trust in the dashboard."""
    doctor_id = await _create_doctor()
    pid, _ = await _create_patient()

    today_noon = datetime.now(timezone.utc).replace(
        hour=12, minute=0, second=0, microsecond=0
    )
    for _ in range(3):
        await _schedule_appointment(
            doctor_id=doctor_id, patient_id=pid, when=today_noon
        )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        digest = await digest_module.build_digest(
            db,
            doctor_id=doctor_id,
            doctor_name="Test",
            when=today_noon,
        )

    counts = digest.summary_counts
    assert counts["appointments_today"] == len(digest.appointments_today)
    assert counts["recap_drafts_pending"] == len(
        digest.recap_drafts_pending
    )
    assert counts["side_effect_reports_24h"] == len(
        digest.side_effect_reports_24h
    )
    assert counts["open_tickets"] == len(digest.open_tickets)


# ---- HTTP endpoint round-trip --------------------------------------------


def test_digest_endpoint_round_trip(orchestrator_client):
    """End-to-end through /doctors/{id}/daily-digest."""
    import asyncio

    loop = asyncio.get_event_loop()
    doctor_id = loop.run_until_complete(_create_doctor())
    pid, _ = loop.run_until_complete(_create_patient())

    today = datetime.now(timezone.utc).replace(
        hour=12, minute=0, second=0, microsecond=0
    )
    loop.run_until_complete(
        _schedule_appointment(
            doctor_id=doctor_id, patient_id=pid, when=today
        )
    )

    r = orchestrator_client.get(
        f"/doctors/{doctor_id}/daily-digest"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["doctor_id"] == doctor_id
    assert "summary_counts" in body
    assert "appointments_today" in body
    assert "recap_drafts_pending" in body
    assert "side_effect_reports_24h" in body
    assert "open_tickets" in body


def test_digest_endpoint_404_for_unknown_doctor(orchestrator_client):
    r = orchestrator_client.get("/doctors/999999999/daily-digest")
    assert r.status_code == 404


def test_digest_endpoint_accepts_explicit_when(orchestrator_client):
    """A doctor in a different timezone can pass an explicit
    ``when`` to align the "today" window to their local day."""
    import asyncio

    loop = asyncio.get_event_loop()
    doctor_id = loop.run_until_complete(_create_doctor())

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    r = orchestrator_client.get(
        f"/doctors/{doctor_id}/daily-digest",
        params={"when": yesterday},
    )
    assert r.status_code == 200
    # ``when`` echoes back so the UI knows what window was used.
    body = r.json()
    assert body["when"].startswith(yesterday[:10])

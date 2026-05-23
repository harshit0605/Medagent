"""Integration tests for the auto visit-brief scheduling flow.

Covers:
    1. ``materialize_for_appointment`` enqueues a single
       ``visit_brief_generate`` event at T-2h.
    2. Same call skips when fire time is already past
       (booking too close to appointment).
    3. ``cancel_for_appointment`` flips pending events to
       skipped — used by the reschedule + cancel paths.
    4. Dispatcher's ``_handle_visit_brief_generate`` invokes
       the generator, persists a brief row, and consumes the
       event.
    5. Dispatcher returns ``None`` (success) on a generator
       LLM failure — the failed audit row is enough; a retry
       would just burn more tokens.
    6. Dispatcher rejects events missing ``patient_db_id`` in
       the payload (defence against malformed enqueues).

DB-required tests are skipped when DATABASE_URL is unset.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.db.models import (
    Appointment,
    AppointmentStatus,
    Doctor,
    DoctorOAuthStatus,
    Patient,
    ScheduledEvent,
    ScheduledEventStatus,
    VisitBrief,
)
from app.db.session import get_sessionmaker
from services.scheduler import dispatcher, visit_brief_scheduling
from services.orchestrator import visit_brief_generator

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping visit-brief-scheduling tests",
)


# ---- shared fixtures -------------------------------------------------------


async def _make_doctor_and_patient(db) -> tuple[Doctor, Patient]:
    suffix = uuid.uuid4().hex[:8]
    doctor = Doctor(
        name=f"Dr Brief {suffix}",
        email=f"vbsched-{suffix}@test.local",
        timezone="Asia/Kolkata",
        calendar_id="primary",
        oauth_status=DoctorOAuthStatus.connected,
    )
    db.add(doctor)
    patient = Patient(
        full_name=f"Brief Sched {suffix}",
        phone=f"vbsched-{suffix}",
    )
    db.add(patient)
    await db.flush()
    await db.refresh(doctor)
    await db.refresh(patient)
    return doctor, patient


async def _make_appointment(
    db, *, doctor: Doctor, patient: Patient, start: datetime
) -> Appointment:
    appt = Appointment(
        patient_id=patient.id,
        doctor_id=doctor.id,
        scheduled_for=start,
        end_at=start + timedelta(minutes=30),
        status=AppointmentStatus.confirmed,
        calendar_event_id=f"evt-{uuid.uuid4().hex[:8]}",
        calendar_html_link=None,
        summary="Test consult",
        source="integration_test",
    )
    db.add(appt)
    await db.flush()
    await db.refresh(appt)
    return appt


async def _cleanup(
    db, *, doctor_id: int, patient_id: int, patient_phone: str
) -> None:
    await db.execute(
        delete(VisitBrief).where(VisitBrief.patient_id == patient_id)
    )
    await db.execute(
        delete(ScheduledEvent).where(
            ScheduledEvent.patient_id == patient_phone
        )
    )
    await db.execute(
        delete(Appointment).where(Appointment.doctor_id == doctor_id)
    )
    await db.execute(delete(Patient).where(Patient.id == patient_id))
    await db.execute(delete(Doctor).where(Doctor.id == doctor_id))
    await db.commit()


# ---- materialize tests -----------------------------------------------------


async def test_materialize_enqueues_event_at_t_minus_2h():
    """Far-future appointment produces exactly one
    ``visit_brief_generate`` event firing 2h before start."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        doctor, patient = await _make_doctor_and_patient(db)
        start = datetime.now(timezone.utc) + timedelta(hours=24)
        appt = await _make_appointment(
            db, doctor=doctor, patient=patient, start=start
        )

        try:
            row = await visit_brief_scheduling.materialize_for_appointment(
                db, appt, patient_phone=patient.phone
            )
            await db.commit()

            assert row is not None
            assert row.event_type == "visit_brief_generate"
            # Within a second of T-2h (Postgres rounding).
            expected = start - timedelta(hours=2)
            actual_scheduled = row.scheduled_for
            if actual_scheduled.tzinfo is None:
                actual_scheduled = actual_scheduled.replace(
                    tzinfo=timezone.utc
                )
            diff = abs((actual_scheduled - expected).total_seconds())
            assert diff < 2.0
            assert row.payload["appointment_id"] == appt.id
            assert row.payload["doctor_id"] == doctor.id
            assert row.payload["patient_db_id"] == patient.id
        finally:
            await _cleanup(
                db,
                doctor_id=doctor.id,
                patient_id=patient.id,
                patient_phone=patient.phone,
            )


async def test_materialize_skips_when_fire_time_in_past():
    """Booking 30 minutes before the appointment means T-2h is
    already past — no point enqueuing an event we'd just have
    to mark stale."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        doctor, patient = await _make_doctor_and_patient(db)
        # Appointment in 30 min → T-2h is 90 min ago.
        start = datetime.now(timezone.utc) + timedelta(minutes=30)
        appt = await _make_appointment(
            db, doctor=doctor, patient=patient, start=start
        )

        try:
            row = await visit_brief_scheduling.materialize_for_appointment(
                db, appt, patient_phone=patient.phone
            )
            assert row is None
        finally:
            await _cleanup(
                db,
                doctor_id=doctor.id,
                patient_id=patient.id,
                patient_phone=patient.phone,
            )


async def test_cancel_marks_pending_event_skipped():
    """Cancellation/reschedule flips the still-pending event
    to ``skipped`` so the dispatcher won't fire it."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        doctor, patient = await _make_doctor_and_patient(db)
        start = datetime.now(timezone.utc) + timedelta(hours=24)
        appt = await _make_appointment(
            db, doctor=doctor, patient=patient, start=start
        )

        try:
            await visit_brief_scheduling.materialize_for_appointment(
                db, appt, patient_phone=patient.phone
            )
            await db.commit()

            count = await visit_brief_scheduling.cancel_for_appointment(
                db,
                appointment_id=appt.id,
                patient_phone=patient.phone,
                reason="test_cancel",
            )
            await db.commit()
            assert count == 1

            rows = list(
                (
                    await db.execute(
                        select(ScheduledEvent).where(
                            ScheduledEvent.event_type
                            == "visit_brief_generate"
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(rows) == 1
            assert rows[0].status == ScheduledEventStatus.skipped
            assert "test_cancel" in (rows[0].error or "")
        finally:
            await _cleanup(
                db,
                doctor_id=doctor.id,
                patient_id=patient.id,
                patient_phone=patient.phone,
            )


async def test_cancel_only_targets_named_appointment():
    """Two appointments → cancel for one shouldn't touch the
    other's pending event."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        doctor, patient = await _make_doctor_and_patient(db)
        start_a = datetime.now(timezone.utc) + timedelta(hours=24)
        start_b = datetime.now(timezone.utc) + timedelta(hours=48)
        appt_a = await _make_appointment(
            db, doctor=doctor, patient=patient, start=start_a
        )
        appt_b = await _make_appointment(
            db, doctor=doctor, patient=patient, start=start_b
        )

        try:
            await visit_brief_scheduling.materialize_for_appointment(
                db, appt_a, patient_phone=patient.phone
            )
            await visit_brief_scheduling.materialize_for_appointment(
                db, appt_b, patient_phone=patient.phone
            )
            await db.commit()

            cancelled = await visit_brief_scheduling.cancel_for_appointment(
                db,
                appointment_id=appt_a.id,
                patient_phone=patient.phone,
            )
            await db.commit()
            assert cancelled == 1

            rows = list(
                (
                    await db.execute(
                        select(ScheduledEvent).where(
                            ScheduledEvent.event_type
                            == "visit_brief_generate"
                        )
                    )
                )
                .scalars()
                .all()
            )
            by_status = {
                r.payload["appointment_id"]: r.status for r in rows
            }
            assert by_status[appt_a.id] == ScheduledEventStatus.skipped
            assert by_status[appt_b.id] == ScheduledEventStatus.pending
        finally:
            await _cleanup(
                db,
                doctor_id=doctor.id,
                patient_id=patient.id,
                patient_phone=patient.phone,
            )


# ---- dispatcher branch tests -----------------------------------------------


async def test_dispatcher_handles_visit_brief_generate_event():
    """Full dispatch path: enqueue an event with fire time in
    the past so the sweeper would normally pick it up, then
    invoke ``dispatch`` directly. Must persist a brief row and
    return None (success)."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        doctor, patient = await _make_doctor_and_patient(db)
        start = datetime.now(timezone.utc) + timedelta(hours=24)
        appt = await _make_appointment(
            db, doctor=doctor, patient=patient, start=start
        )

        try:
            # Enqueue with a past fire time so we don't depend
            # on real-time wall clock.
            event = ScheduledEvent(
                event_type="visit_brief_generate",
                patient_id=patient.phone,
                payload={
                    "appointment_id": appt.id,
                    "doctor_id": doctor.id,
                    "patient_db_id": patient.id,
                    "appointment_start_iso": start.isoformat(),
                },
                scheduled_for=datetime.now(timezone.utc)
                - timedelta(minutes=5),
                status=ScheduledEventStatus.pending,
            )
            db.add(event)
            await db.flush()
            await db.commit()

            result = await dispatcher.dispatch(event, db=db)
            assert result is None  # success

            briefs = list(
                (
                    await db.execute(
                        select(VisitBrief).where(
                            VisitBrief.patient_id == patient.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(briefs) == 1
            brief = briefs[0]
            assert brief.appointment_id == appt.id
            assert brief.doctor_id == doctor.id
            assert brief.generated_by == "scheduler.auto"
        finally:
            await _cleanup(
                db,
                doctor_id=doctor.id,
                patient_id=patient.id,
                patient_phone=patient.phone,
            )


async def test_dispatcher_consumes_event_when_llm_fails(monkeypatch):
    """When the LLM raises, the dispatcher returns None
    anyway — the generator already wrote a failed-brief audit
    row and retrying would just burn more tokens."""

    class _FailingLLM:
        enabled = True
        model = "gpt-4o-mini"

        def _get_client(self):
            raise RuntimeError("simulated LLM down")

    monkeypatch.setattr(
        visit_brief_generator, "get_llm", lambda: _FailingLLM()
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        doctor, patient = await _make_doctor_and_patient(db)
        start = datetime.now(timezone.utc) + timedelta(hours=24)
        appt = await _make_appointment(
            db, doctor=doctor, patient=patient, start=start
        )

        try:
            event = ScheduledEvent(
                event_type="visit_brief_generate",
                patient_id=patient.phone,
                payload={
                    "appointment_id": appt.id,
                    "doctor_id": doctor.id,
                    "patient_db_id": patient.id,
                    "appointment_start_iso": start.isoformat(),
                },
                scheduled_for=datetime.now(timezone.utc)
                - timedelta(minutes=5),
                status=ScheduledEventStatus.pending,
            )
            db.add(event)
            await db.flush()
            await db.commit()

            result = await dispatcher.dispatch(event, db=db)
            assert result is None  # consumed despite LLM failure

            failed = list(
                (
                    await db.execute(
                        select(VisitBrief).where(
                            VisitBrief.patient_id == patient.id,
                            VisitBrief.error.is_not(None),
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(failed) == 1
            assert "simulated LLM down" in (failed[0].error or "")
        finally:
            await _cleanup(
                db,
                doctor_id=doctor.id,
                patient_id=patient.id,
                patient_phone=patient.phone,
            )


async def test_dispatcher_rejects_event_without_patient_db_id():
    """Defence in depth — a malformed payload returns an
    ``unmapped:`` string so the scheduler tick marks it failed
    rather than swallowing silently."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        doctor, patient = await _make_doctor_and_patient(db)
        try:
            event = ScheduledEvent(
                event_type="visit_brief_generate",
                patient_id=patient.phone,
                # Missing patient_db_id — bug in some hypothetical
                # caller path.
                payload={"appointment_id": 1, "doctor_id": 1},
                scheduled_for=datetime.now(timezone.utc)
                - timedelta(minutes=5),
                status=ScheduledEventStatus.pending,
            )
            db.add(event)
            await db.flush()
            await db.commit()

            result = await dispatcher.dispatch(event, db=db)
            assert result is not None
            assert result.startswith("unmapped:")
        finally:
            await _cleanup(
                db,
                doctor_id=doctor.id,
                patient_id=patient.id,
                patient_phone=patient.phone,
            )

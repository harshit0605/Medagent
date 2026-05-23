"""Integration tests for appointment reminder materialize / cancel.

Hits the real Supabase DB via SessionLocal — skipped without DATABASE_URL.
Each test cleans up its own scheduled_events / appointments rows.
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
)
from app.db.session import get_sessionmaker
from services.scheduler import appointment_reminders

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping appointment reminder integration tests",
)


async def _make_doctor_and_patient(db) -> tuple[Doctor, Patient]:
    suffix = uuid.uuid4().hex[:8]
    doctor = Doctor(
        name=f"Dr Test {suffix}",
        email=f"dr-{suffix}@test.local",
        timezone="Asia/Kolkata",
        calendar_id="primary",
        oauth_status=DoctorOAuthStatus.connected,
    )
    db.add(doctor)
    patient = Patient(
        full_name=f"Test Patient {suffix}",
        phone=f"reminder-test-{suffix}",
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
        summary="Test consultation",
        source="integration_test",
    )
    db.add(appt)
    await db.flush()
    await db.refresh(appt)
    return appt


async def _cleanup(db, *, doctor_id: int, patient_id: int, patient_phone: str) -> None:
    await db.execute(
        delete(ScheduledEvent).where(ScheduledEvent.patient_id == patient_phone)
    )
    await db.execute(delete(Appointment).where(Appointment.doctor_id == doctor_id))
    await db.execute(delete(Patient).where(Patient.id == patient_id))
    await db.execute(delete(Doctor).where(Doctor.id == doctor_id))
    await db.commit()


async def test_materialize_creates_two_pending_reminders():
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        doctor, patient = await _make_doctor_and_patient(db)
        # 48 hours in the future — both T-24h and T-1h are valid future times.
        start = datetime.now(timezone.utc) + timedelta(hours=48)
        appt = await _make_appointment(db, doctor=doctor, patient=patient, start=start)

        try:
            created = await appointment_reminders.materialize_for_appointment(
                db, appt, patient_phone=patient.phone
            )
            await db.commit()

            assert len(created) == 2
            event_types = {row.event_type for row in created}
            assert event_types == {
                "appointment_reminder_24h",
                "appointment_reminder_1h",
            }

            # Re-query to confirm persistence.
            stmt = select(ScheduledEvent).where(
                ScheduledEvent.patient_id == patient.phone,
                ScheduledEvent.status == ScheduledEventStatus.pending,
            )
            persisted = list((await db.execute(stmt)).scalars().all())
            assert len(persisted) == 2
            for row in persisted:
                assert row.payload["appointment_id"] == appt.id
                assert row.payload["doctor_id"] == doctor.id
                assert row.payload["patient_db_id"] == patient.id
                assert row.payload["appointment_start_iso"]
        finally:
            await _cleanup(
                db,
                doctor_id=doctor.id,
                patient_id=patient.id,
                patient_phone=patient.phone,
            )


async def test_materialize_skips_reminders_in_the_past():
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        doctor, patient = await _make_doctor_and_patient(db)
        # Appointment in 30 minutes — T-24h is in the past, T-1h would also
        # be in the past, so we expect zero rows.
        start = datetime.now(timezone.utc) + timedelta(minutes=30)
        appt = await _make_appointment(db, doctor=doctor, patient=patient, start=start)

        try:
            created = await appointment_reminders.materialize_for_appointment(
                db, appt, patient_phone=patient.phone
            )
            assert created == []
        finally:
            await _cleanup(
                db,
                doctor_id=doctor.id,
                patient_id=patient.id,
                patient_phone=patient.phone,
            )


async def test_materialize_keeps_only_one_h_when_close_to_appointment():
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        doctor, patient = await _make_doctor_and_patient(db)
        # In 90 minutes: T-24h is in the past (skip); T-1h is +30min from now (keep).
        start = datetime.now(timezone.utc) + timedelta(minutes=90)
        appt = await _make_appointment(db, doctor=doctor, patient=patient, start=start)

        try:
            created = await appointment_reminders.materialize_for_appointment(
                db, appt, patient_phone=patient.phone
            )
            assert len(created) == 1
            assert created[0].event_type == "appointment_reminder_1h"
        finally:
            await _cleanup(
                db,
                doctor_id=doctor.id,
                patient_id=patient.id,
                patient_phone=patient.phone,
            )


async def test_cancel_marks_pending_reminders_skipped():
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        doctor, patient = await _make_doctor_and_patient(db)
        start = datetime.now(timezone.utc) + timedelta(hours=48)
        appt = await _make_appointment(db, doctor=doctor, patient=patient, start=start)

        try:
            await appointment_reminders.materialize_for_appointment(
                db, appt, patient_phone=patient.phone
            )
            await db.commit()

            cancelled_count = await appointment_reminders.cancel_for_appointment(
                db,
                appointment_id=appt.id,
                patient_phone=patient.phone,
                reason="test_cancellation",
            )
            await db.commit()
            assert cancelled_count == 2

            stmt = select(ScheduledEvent).where(
                ScheduledEvent.patient_id == patient.phone,
            )
            rows = list((await db.execute(stmt)).scalars().all())
            assert len(rows) == 2
            assert all(row.status == ScheduledEventStatus.skipped for row in rows)
            assert all(row.error == "test_cancellation" for row in rows)
        finally:
            await _cleanup(
                db,
                doctor_id=doctor.id,
                patient_id=patient.id,
                patient_phone=patient.phone,
            )


async def test_cancel_only_touches_matching_appointment():
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        doctor, patient = await _make_doctor_and_patient(db)
        start_a = datetime.now(timezone.utc) + timedelta(hours=48)
        start_b = datetime.now(timezone.utc) + timedelta(hours=72)
        appt_a = await _make_appointment(
            db, doctor=doctor, patient=patient, start=start_a
        )
        appt_b = await _make_appointment(
            db, doctor=doctor, patient=patient, start=start_b
        )

        try:
            await appointment_reminders.materialize_for_appointment(
                db, appt_a, patient_phone=patient.phone
            )
            await appointment_reminders.materialize_for_appointment(
                db, appt_b, patient_phone=patient.phone
            )
            await db.commit()

            # Cancel only appt_a's reminders.
            n = await appointment_reminders.cancel_for_appointment(
                db, appointment_id=appt_a.id, patient_phone=patient.phone
            )
            await db.commit()
            assert n == 2

            stmt = select(ScheduledEvent).where(
                ScheduledEvent.patient_id == patient.phone,
                ScheduledEvent.status == ScheduledEventStatus.pending,
            )
            still_pending = list((await db.execute(stmt)).scalars().all())
            assert len(still_pending) == 2
            assert all(
                row.payload["appointment_id"] == appt_b.id for row in still_pending
            )
        finally:
            await _cleanup(
                db,
                doctor_id=doctor.id,
                patient_id=patient.id,
                patient_phone=patient.phone,
            )

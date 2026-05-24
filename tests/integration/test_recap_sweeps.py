"""Integration tests for the recap-lifecycle sweeps.

Covers:
- ``sweep_missing_recaps`` opens a single ops ticket per patient when an
  appointment is past its grace window with no recap, and is idempotent.
- ``sweep_unacked_recaps`` enqueues a one-time ack-nudge ScheduledEvent
  for sent-but-unacked recaps and skips on repeat.
- The dispatcher's ``_build_recap_ack_nudge`` path renders the right
  freeform / template variant by CSW status, and skips when the recap
  has been acked between sweep and dispatch.

Skipped when DATABASE_URL is unset.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db.models import (
    Appointment,
    AppointmentRecap,
    AppointmentStatus,
    Doctor,
    DoctorOAuthStatus,
    OpsTicket,
    Patient,
    PatientInboundState,
    RecapStatus,
    ScheduledEvent,
    ScheduledEventStatus,
)
from app.db.repositories import scheduled_events as scheduled_events_repo
from app.db.session import get_sessionmaker
from services.scheduler import recap_sweeps
from services.scheduler.dispatcher import (
    ReminderNotApplicable,
    _build_recap_ack_nudge,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping recap sweep integration tests",
)


async def _seed_patient_doctor() -> tuple[Patient, Doctor]:
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        patient = Patient(
            full_name=f"Sweep Test {suffix}",
            phone=f"sweep-test-{suffix}",
        )
        doctor = Doctor(
            name=f"Dr. Sweep {suffix}",
            email=f"dr-sweep-{suffix}@example.com",
            timezone="UTC",
            calendar_id="primary",
            oauth_status=DoctorOAuthStatus.connected,
        )
        db.add_all([patient, doctor])
        await db.flush()
        await db.commit()
        await db.refresh(patient)
        await db.refresh(doctor)
        return patient, doctor


async def _seed_completed_appointment(
    patient_id: int,
    doctor_id: int,
    *,
    ended_hours_ago: int,
) -> int:
    when_end = datetime.now(timezone.utc) - timedelta(hours=ended_hours_ago)
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        appointment = Appointment(
            patient_id=patient_id,
            doctor_id=doctor_id,
            scheduled_for=when_end - timedelta(minutes=30),
            end_at=when_end,
            status=AppointmentStatus.completed,
            source="test",
        )
        db.add(appointment)
        await db.flush()
        await db.commit()
        return appointment.id


async def _seed_sent_recap(
    appointment_id: int,
    patient_id: int,
    doctor_id: int,
    *,
    sent_hours_ago: int,
    status: RecapStatus = RecapStatus.sent,
) -> int:
    sent_at = datetime.now(timezone.utc) - timedelta(hours=sent_hours_ago)
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        recap = AppointmentRecap(
            appointment_id=appointment_id,
            patient_id=patient_id,
            doctor_id=doctor_id,
            doctor_notes="Test notes",
            structured_payload={},
            generated_text="Hi, here's your recap.",
            status=status,
            sent_at=sent_at,
            sent_message_id=f"wamid.test.{uuid.uuid4().hex[:8]}",
        )
        db.add(recap)
        await db.flush()
        await db.commit()
        return recap.id


# ---- Missing-recap sweep --------------------------------------------------


async def test_sweep_missing_recaps_opens_ticket():
    patient, doctor = await _seed_patient_doctor()
    appt_id = await _seed_completed_appointment(
        patient.id, doctor.id, ended_hours_ago=48
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await recap_sweeps.sweep_missing_recaps(db)
        await db.commit()
    assert result["opened"] >= 1

    # Verify ops ticket exists for this patient.
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(OpsTicket).where(
                    OpsTicket.patient_id == patient.phone,
                    OpsTicket.category == "recap_missing",
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    assert f"Appointment #{appt_id}" in (rows[0].notes or "")


async def test_sweep_missing_recaps_idempotent_with_open_ticket():
    patient, doctor = await _seed_patient_doctor()
    await _seed_completed_appointment(
        patient.id, doctor.id, ended_hours_ago=48
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        first = await recap_sweeps.sweep_missing_recaps(db)
        await db.commit()
        second = await recap_sweeps.sweep_missing_recaps(db)
        await db.commit()
    assert first["opened"] == 1
    assert second["opened"] == 0
    assert second["skipped_existing"] >= 1


async def test_sweep_missing_recaps_respects_grace_window():
    """Appointment that ended only 2 hours ago shouldn't trigger — the
    doctor might still be charting."""
    patient, doctor = await _seed_patient_doctor()
    await _seed_completed_appointment(
        patient.id, doctor.id, ended_hours_ago=2
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await recap_sweeps.sweep_missing_recaps(db)
        await db.commit()
    assert result["opened"] == 0


# ---- Unacked-recap nudge sweep -------------------------------------------


async def test_sweep_unacked_recaps_enqueues_nudge():
    patient, doctor = await _seed_patient_doctor()
    appt_id = await _seed_completed_appointment(
        patient.id, doctor.id, ended_hours_ago=72
    )
    recap_id = await _seed_sent_recap(
        appt_id, patient.id, doctor.id, sent_hours_ago=48
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await recap_sweeps.sweep_unacked_recaps(db)
        await db.commit()
    assert result["enqueued"] >= 1

    async with SessionLocal() as db:
        events = (
            await db.execute(
                select(ScheduledEvent).where(
                    ScheduledEvent.event_type == "recap_ack_nudge",
                    ScheduledEvent.patient_id == patient.phone,
                )
            )
        ).scalars().all()
    assert len(events) == 1
    assert events[0].payload["recap_id"] == recap_id
    assert events[0].status == ScheduledEventStatus.pending


async def test_sweep_unacked_recaps_idempotent():
    patient, doctor = await _seed_patient_doctor()
    appt_id = await _seed_completed_appointment(
        patient.id, doctor.id, ended_hours_ago=72
    )
    await _seed_sent_recap(
        appt_id, patient.id, doctor.id, sent_hours_ago=48
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        first = await recap_sweeps.sweep_unacked_recaps(db)
        await db.commit()
        second = await recap_sweeps.sweep_unacked_recaps(db)
        await db.commit()
    assert first["enqueued"] == 1
    assert second["enqueued"] == 0
    assert second["skipped"] >= 1


async def test_sweep_unacked_recaps_key_dedupes_past_status_check():
    """The per-recap idempotency key closes the TOCTOU race the status
    pre-check can't: even after a prior ack-nudge leaves the
    pending/dispatched set (so _has_existing_ack_nudge is False), a second
    enqueue with the same key is rejected — no duplicate nudge."""
    patient, doctor = await _seed_patient_doctor()
    appt_id = await _seed_completed_appointment(
        patient.id, doctor.id, ended_hours_ago=72
    )
    recap_id = await _seed_sent_recap(
        appt_id, patient.id, doctor.id, sent_hours_ago=48
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        # Pre-create the ack-nudge with the sweep's key, then FAIL it so the
        # status pre-check (pending/dispatched only) no longer sees it.
        ev = await scheduled_events_repo.enqueue_idempotent(
            db,
            event_type=recap_sweeps.RECAP_ACK_NUDGE_EVENT_TYPE,
            patient_id=patient.phone,
            payload={"recap_id": recap_id, "appointment_id": appt_id},
            idempotency_key=f"recap_ack_nudge:{recap_id}",
        )
        assert ev is not None
        await scheduled_events_repo.mark_failed(db, ev.id, error="boom")
        await db.commit()

        result = await recap_sweeps.sweep_unacked_recaps(db)
        await db.commit()

    # Status check is bypassed (failed != pending/dispatched), but the key
    # blocks the duplicate enqueue.
    assert result["enqueued"] == 0
    assert result["skipped"] >= 1


async def test_sweep_unacked_recaps_skips_acked():
    patient, doctor = await _seed_patient_doctor()
    appt_id = await _seed_completed_appointment(
        patient.id, doctor.id, ended_hours_ago=72
    )
    await _seed_sent_recap(
        appt_id,
        patient.id,
        doctor.id,
        sent_hours_ago=48,
        status=RecapStatus.acknowledged,
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await recap_sweeps.sweep_unacked_recaps(db)
        await db.commit()
    assert result["enqueued"] == 0


# ---- Dispatcher build path ------------------------------------------------


async def test_build_recap_ack_nudge_freeform_when_in_csw():
    patient, doctor = await _seed_patient_doctor()
    appt_id = await _seed_completed_appointment(
        patient.id, doctor.id, ended_hours_ago=72
    )
    recap_id = await _seed_sent_recap(
        appt_id, patient.id, doctor.id, sent_hours_ago=48
    )

    # Force in-CSW by inserting a recent inbound state row.
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        db.add(
            PatientInboundState(
                patient_id=patient.phone,
                last_inbound_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()

    event = ScheduledEvent(
        event_type="recap_ack_nudge",
        patient_id=patient.phone,
        scheduled_for=datetime.now(timezone.utc),
        payload={"recap_id": recap_id, "appointment_id": appt_id},
    )
    async with SessionLocal() as db:
        message = await _build_recap_ack_nudge(db, event)
    assert message["use_template"] is False
    quick_reply_titles = {qr["title"] for qr in message.get("quick_replies", [])}
    assert quick_reply_titles == {"Got it", "I have a question"}
    assert "did you see" in message["body"].lower()


async def test_build_recap_ack_nudge_template_when_out_of_csw():
    patient, doctor = await _seed_patient_doctor()
    appt_id = await _seed_completed_appointment(
        patient.id, doctor.id, ended_hours_ago=72
    )
    recap_id = await _seed_sent_recap(
        appt_id, patient.id, doctor.id, sent_hours_ago=48
    )

    # No inbound state row → out-of-CSW.
    event = ScheduledEvent(
        event_type="recap_ack_nudge",
        patient_id=patient.phone,
        scheduled_for=datetime.now(timezone.utc),
        payload={"recap_id": recap_id, "appointment_id": appt_id},
    )
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        message = await _build_recap_ack_nudge(db, event)
    assert message["use_template"] is True
    assert message["template_name"] == os.getenv(
        "WHATSAPP_RECAP_TEMPLATE_NAME", "post_visit_recap_v1"
    )
    # Template params: 1_name carries the patient's first name.
    assert "1_name" in message["template_params"]


async def test_build_recap_ack_nudge_skips_if_acked_meanwhile():
    patient, doctor = await _seed_patient_doctor()
    appt_id = await _seed_completed_appointment(
        patient.id, doctor.id, ended_hours_ago=72
    )
    recap_id = await _seed_sent_recap(
        appt_id,
        patient.id,
        doctor.id,
        sent_hours_ago=48,
        status=RecapStatus.acknowledged,
    )

    event = ScheduledEvent(
        event_type="recap_ack_nudge",
        patient_id=patient.phone,
        scheduled_for=datetime.now(timezone.utc),
        payload={"recap_id": recap_id, "appointment_id": appt_id},
    )
    SessionLocal = get_sessionmaker()
    with pytest.raises(ReminderNotApplicable):
        async with SessionLocal() as db:
            await _build_recap_ack_nudge(db, event)

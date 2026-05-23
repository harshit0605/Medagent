"""Materialize / cancel T-24h and T-1h appointment reminders.

Each confirmed appointment gets two rows in ``scheduled_events``:
``appointment_reminder_24h`` and ``appointment_reminder_1h``. The scheduler's
poll loop picks them up at the appointed time and the dispatcher renders a
freeform WhatsApp body (see :mod:`services.scheduler.dispatcher`).

When an appointment is cancelled or rescheduled, the booking agent calls
:func:`cancel_for_appointment` to mark any still-pending reminders as
``skipped`` so we don't ping the patient about a booking that no longer
exists. Reschedules then call :func:`materialize_for_appointment` again
against the new time.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Appointment, ScheduledEvent, ScheduledEventStatus
from app.db.repositories import patients as patients_repo
from app.db.repositories import scheduled_events as scheduled_events_repo

log = logging.getLogger(__name__)


REMINDER_OFFSETS: tuple[tuple[str, int], ...] = (
    ("appointment_reminder_24h", 24),
    ("appointment_reminder_1h", 1),
)
REMINDER_EVENT_TYPES: tuple[str, ...] = tuple(et for et, _ in REMINDER_OFFSETS)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def materialize_for_appointment(
    db: AsyncSession,
    appointment: Appointment,
    *,
    patient_phone: str | None = None,
    now: datetime | None = None,
) -> list[ScheduledEvent]:
    """Enqueue T-24h + T-1h reminder rows for ``appointment``.

    Skips any offset whose firing time is already past — booking 30 min before
    the appointment correctly drops the T-1h reminder but still queues T-24h
    for the *next* day's appointment of the same type.

    ``patient_phone`` is the WhatsApp wa_id we ship to Meta. When omitted we
    look it up from ``appointment.patient_id``; pass it explicitly when the
    caller already has it (booking agent path) to skip the round-trip.
    """
    if patient_phone is None:
        patient = await patients_repo.get(db, appointment.patient_id)
        if patient is None:
            log.warning(
                "appointment %s: patient %s missing — skipping reminders",
                appointment.id,
                appointment.patient_id,
            )
            return []
        patient_phone = patient.phone

    when_now = now or datetime.now(timezone.utc)
    appt_start = _ensure_utc(appointment.scheduled_for)

    out: list[ScheduledEvent] = []
    for event_type, offset_hours in REMINDER_OFFSETS:
        scheduled_for = appt_start - timedelta(hours=offset_hours)
        if scheduled_for <= when_now:
            log.info(
                "skip %s for appt=%s — fires in past (%s)",
                event_type,
                appointment.id,
                scheduled_for.isoformat(),
            )
            continue
        row = await scheduled_events_repo.enqueue(
            db,
            event_type=event_type,
            patient_id=patient_phone,
            payload={
                "appointment_id": appointment.id,
                "doctor_id": appointment.doctor_id,
                "patient_db_id": appointment.patient_id,
                "appointment_start_iso": appt_start.isoformat(),
            },
            scheduled_for=scheduled_for,
        )
        out.append(row)
    return out


async def cancel_for_appointment(
    db: AsyncSession,
    *,
    appointment_id: int,
    patient_phone: str | None = None,
    reason: str = "appointment_cancelled",
) -> int:
    """Mark every pending appointment_reminder_* row for this appointment as skipped.

    ``patient_phone`` is an optional scoping hint — when provided the SQL
    query also filters on ``patient_id`` to keep the working set small. The
    Python-side ``payload['appointment_id']`` check is the authoritative
    match (a single patient has at most a handful of pending reminders, so
    O(N) here is fine).
    """
    stmt = (
        select(ScheduledEvent)
        .where(ScheduledEvent.event_type.in_(REMINDER_EVENT_TYPES))
        .where(ScheduledEvent.status == ScheduledEventStatus.pending)
    )
    if patient_phone:
        stmt = stmt.where(ScheduledEvent.patient_id == patient_phone)
    rows = list((await db.execute(stmt)).scalars().all())

    cancelled = 0
    for row in rows:
        if (row.payload or {}).get("appointment_id") == appointment_id:
            row.status = ScheduledEventStatus.skipped
            row.error = reason[:1000]
            cancelled += 1
    if cancelled:
        await db.flush()
    return cancelled

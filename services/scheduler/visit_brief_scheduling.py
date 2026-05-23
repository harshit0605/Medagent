"""Materialize / cancel auto-generation events for visit briefs.

Mirrors ``appointment_reminders`` in shape: when an
appointment is confirmed, enqueue a ``visit_brief_generate``
event for T-2h before. Reschedules cancel the pending event
and re-materialise against the new start. Cancellations skip
the pending event so we don't burn LLM tokens on a visit that
won't happen.

Why T-2h? Two competing pressures:

    - Too far ahead and the brief misses late-breaking events
      (a 6am missed dose right before an 8am appointment is
      exactly the thing the doctor should know about).
    - Too close and a slow LLM call holds up the doctor when
      they actually want to read the brief.

Two hours splits the difference: the sweep + dispatcher cycle
(60s + LLM call) finishes comfortably inside the window, and
events later than T-2h are rare.

The dispatcher's ``visit_brief_generate`` branch (see
``services/scheduler/dispatcher.py``) calls the generator
directly — these events do NOT produce an outbound WhatsApp
message, so they bypass the gateway. The dispatcher's stale +
opt-out gates are also irrelevant for an internal-only event,
which is why this module lives next to its peers but the
dispatch path is special-cased.
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


VISIT_BRIEF_EVENT_TYPE = "visit_brief_generate"

# Generation lead time. Tunable via env if a real-world
# deployment finds 2h too tight or too generous, but the
# default is the production-grade choice.
LEAD_TIME = timedelta(hours=2)


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
) -> ScheduledEvent | None:
    """Enqueue a single ``visit_brief_generate`` event for the
    appointment, firing at T-2h. Returns the row, or ``None``
    when the firing time is already past (booking too late) —
    a same-day on-demand brief is the operator's recourse.

    ``patient_phone`` matches the convention from
    ``appointment_reminders``; we don't strictly need it for
    the brief (the generator keys off ``patient_db_id``) but
    keeping the column populated lets the same patient-scoped
    cancel queries work without a join.
    """
    if patient_phone is None:
        patient = await patients_repo.get(db, appointment.patient_id)
        if patient is None:
            log.warning(
                "appointment %s: patient %s missing — skipping brief schedule",
                appointment.id,
                appointment.patient_id,
            )
            return None
        patient_phone = patient.phone

    when_now = now or datetime.now(timezone.utc)
    appt_start = _ensure_utc(appointment.scheduled_for)
    fire_at = appt_start - LEAD_TIME

    if fire_at <= when_now:
        log.info(
            "skip visit_brief_generate for appt=%s — fires in past (%s)",
            appointment.id,
            fire_at.isoformat(),
        )
        return None

    row = await scheduled_events_repo.enqueue(
        db,
        event_type=VISIT_BRIEF_EVENT_TYPE,
        patient_id=patient_phone,
        payload={
            "appointment_id": appointment.id,
            "doctor_id": appointment.doctor_id,
            "patient_db_id": appointment.patient_id,
            "appointment_start_iso": appt_start.isoformat(),
        },
        scheduled_for=fire_at,
    )
    return row


async def cancel_for_appointment(
    db: AsyncSession,
    *,
    appointment_id: int,
    patient_phone: str | None = None,
    reason: str = "appointment_cancelled",
) -> int:
    """Mark every pending ``visit_brief_generate`` row for this
    appointment as skipped. Mirrors the reminder cancel.
    Returns the count flipped so callers can log churn."""
    stmt = (
        select(ScheduledEvent)
        .where(ScheduledEvent.event_type == VISIT_BRIEF_EVENT_TYPE)
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

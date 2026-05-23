"""Sweeps for the after-visit recap lifecycle.

Two passes, each idempotent and cheap:

- ``sweep_missing_recaps``: completed appointments older than the grace
  window with no recap row open an ops_ticket so a clinician finishes
  the recap. Idempotent against an already-open ``recap_missing`` ticket
  for the same appointment.

- ``sweep_unacked_recaps``: recaps in ``sent`` status whose sent_at is
  past the nudge window get a one-time gentle WhatsApp nudge ("just
  checking — did you see the recap?"). The nudge is fire-and-forget via
  a ScheduledEvent row of type ``recap_ack_nudge`` so the dispatcher
  owns delivery (and respects in-CSW vs template fallback). Each recap
  is nudged at most once: we mark the recap with a sentinel by setting
  ``acknowledged_at`` to the nudge timestamp via the ``mark_questioned``
  state? No — we don't want to taint state. Instead we look for an
  existing ``recap_ack_nudge`` ScheduledEvent referencing this recap
  and skip if present.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Appointment,
    AppointmentRecap,
    AppointmentStatus,
    RecapStatus,
    ScheduledEvent,
    ScheduledEventStatus,
)
from app.db.repositories import ops_tickets as ops_tickets_repo
from app.db.repositories import patients as patients_repo
from app.db.repositories import scheduled_events as scheduled_events_repo

log = logging.getLogger(__name__)


# How long after an appointment we tolerate a missing recap before
# pinging ops. Must be long enough that the doctor has finished post-
# visit charting and short enough that the patient still benefits.
DEFAULT_RECAP_MISSING_GRACE = timedelta(hours=24)

# How long after a recap is sent we wait before nudging an unacked
# patient. Should be long enough to feel patient-friendly, not pushy.
DEFAULT_ACK_NUDGE_DELAY = timedelta(hours=24)

RECAP_MISSING_CATEGORY = "recap_missing"
RECAP_MISSING_PRIORITY = "p3"
RECAP_MISSING_SLA_MINUTES = 1440  # 24h
RECAP_ACK_NUDGE_EVENT_TYPE = "recap_ack_nudge"


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _find_appointments_missing_recap(
    db: AsyncSession, *, older_than: datetime
) -> list[Appointment]:
    """Completed/confirmed appointments whose end_at is older than the
    grace window and have no AppointmentRecap row at all. We INCLUDE
    ``confirmed`` here too because the booking flow doesn't currently
    flip status to ``completed`` automatically — past appointments stay
    confirmed unless someone explicitly cancels."""
    stmt = (
        select(Appointment)
        .outerjoin(
            AppointmentRecap,
            AppointmentRecap.appointment_id == Appointment.id,
        )
        .where(AppointmentRecap.id.is_(None))
        .where(Appointment.end_at <= older_than)
        .where(
            Appointment.status.in_(
                [AppointmentStatus.confirmed, AppointmentStatus.completed]
            )
        )
        # Keep the sweep cheap; the missed-recap pile shouldn't grow
        # unboundedly because each pass opens an ops ticket and we
        # don't re-tick the same appointment once a ticket is open.
        .order_by(Appointment.end_at.desc())
        .limit(200)
    )
    return list((await db.execute(stmt)).scalars().all())


async def sweep_missing_recaps(
    db: AsyncSession,
    *,
    grace_window: timedelta | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """One pass: open ops tickets for appointments that should have a
    recap but don't. Returns counts so the caller can log activity."""
    grace = grace_window or DEFAULT_RECAP_MISSING_GRACE
    when_now = _ensure_utc(now or datetime.now(timezone.utc))
    cutoff = when_now - grace

    candidates = await _find_appointments_missing_recap(db, older_than=cutoff)
    opened = 0
    skipped_existing = 0
    for appointment in candidates:
        patient = await patients_repo.get(db, appointment.patient_id)
        if patient is None:
            continue
        existing = await ops_tickets_repo.find_open_for_patient_category(
            db,
            patient_id=patient.phone,
            category=RECAP_MISSING_CATEGORY,
        )
        # Two-step idempotency:
        #   - If ANY open recap_missing ticket exists for the patient,
        #     skip. Multiple unrecapped visits in flight should still
        #     surface as one item — they're a single workflow problem.
        if existing is not None:
            skipped_existing += 1
            continue
        await ops_tickets_repo.create(
            db,
            patient_id=patient.phone,
            category=RECAP_MISSING_CATEGORY,
            priority=RECAP_MISSING_PRIORITY,
            sla_minutes=RECAP_MISSING_SLA_MINUTES,
            notes=(
                f"Appointment #{appointment.id} ended "
                f"{appointment.end_at.isoformat()} with no after-visit recap. "
                f"Compose one at /appointments/{appointment.id}/recap."
            ),
        )
        opened += 1
    if opened:
        await db.flush()
    return {
        "opened": opened,
        "skipped_existing": skipped_existing,
        "candidates": len(candidates),
    }


async def _find_unacked_recaps_due_for_nudge(
    db: AsyncSession, *, sent_before: datetime
) -> list[AppointmentRecap]:
    """Recaps in ``sent`` (NOT ``acknowledged`` or ``questioned``) that
    were sent more than the nudge delay ago. We exclude ``questioned``
    because those already have an ops ticket — nudging on top of that
    just adds noise."""
    stmt = (
        select(AppointmentRecap)
        .where(AppointmentRecap.status == RecapStatus.sent)
        .where(AppointmentRecap.sent_at.isnot(None))
        .where(AppointmentRecap.sent_at <= sent_before)
        .order_by(AppointmentRecap.sent_at.asc())
        .limit(100)
    )
    return list((await db.execute(stmt)).scalars().all())


async def _has_existing_ack_nudge(
    db: AsyncSession, *, patient_phone: str, recap_id: int
) -> bool:
    """Has the dispatcher already enqueued (or sent) an ack-nudge for
    this recap? We check pending + dispatched so re-running the sweep
    after a successful nudge doesn't re-queue."""
    stmt = (
        select(ScheduledEvent.id)
        .where(ScheduledEvent.event_type == RECAP_ACK_NUDGE_EVENT_TYPE)
        .where(ScheduledEvent.patient_id == patient_phone)
        .where(ScheduledEvent.status.in_(
            [
                ScheduledEventStatus.pending,
                ScheduledEventStatus.dispatched,
            ]
        ))
        .limit(50)
    )
    rows = (await db.execute(stmt)).scalars().all()
    if not rows:
        return False
    # We can't filter on payload from the SQL query easily; do it in
    # Python over the (tiny) returned set.
    full = (
        await db.execute(
            select(ScheduledEvent).where(ScheduledEvent.id.in_(rows))
        )
    ).scalars().all()
    return any((e.payload or {}).get("recap_id") == recap_id for e in full)


async def sweep_unacked_recaps(
    db: AsyncSession,
    *,
    nudge_delay: timedelta | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Enqueue a one-time ack-nudge ScheduledEvent for each recap whose
    sent_at is older than the nudge delay and which still hasn't been
    acked. Idempotent: a recap that already has an ack-nudge event
    (pending or dispatched) is skipped."""
    delay = nudge_delay or DEFAULT_ACK_NUDGE_DELAY
    when_now = _ensure_utc(now or datetime.now(timezone.utc))
    cutoff = when_now - delay

    candidates = await _find_unacked_recaps_due_for_nudge(
        db, sent_before=cutoff
    )
    enqueued = 0
    skipped = 0
    for recap in candidates:
        patient = await patients_repo.get(db, recap.patient_id)
        if patient is None:
            continue
        already = await _has_existing_ack_nudge(
            db, patient_phone=patient.phone, recap_id=recap.id
        )
        if already:
            skipped += 1
            continue
        await scheduled_events_repo.enqueue(
            db,
            event_type=RECAP_ACK_NUDGE_EVENT_TYPE,
            patient_id=patient.phone,
            payload={
                "recap_id": recap.id,
                "appointment_id": recap.appointment_id,
            },
            scheduled_for=when_now,
        )
        enqueued += 1
    if enqueued:
        await db.flush()
    return {
        "enqueued": enqueued,
        "skipped": skipped,
        "candidates": len(candidates),
    }


async def sweep_recap_lifecycle(
    db: AsyncSession,
    *,
    now: datetime | None = None,
) -> dict[str, dict[str, int]]:
    """Convenience wrapper for the scheduler loop — runs both sweeps
    in one DB session."""
    missing = await sweep_missing_recaps(db, now=now)
    unacked = await sweep_unacked_recaps(db, now=now)
    return {"missing": missing, "unacked": unacked}

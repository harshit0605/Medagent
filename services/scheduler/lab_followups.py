"""Materialize / cancel lab follow-up reminders.

Reminder cadence relative to ``LabFollowup.due_by``:

  - T-7d  ("d7")     — early heads-up
  - T-1d  ("d1")     — day-before nudge
  - T+2d  ("overdue") — chase if still not completed

All three stages fire at 09:00 in the regimen's default timezone. Cycle
isolation keys reuse ``lab_followup_id`` since each followup is its own
cycle (unlike refills which can repeat). When the patient taps Completed
the orchestrator calls :func:`cancel_for_lab_followup` to skip pending
reminders.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    LabFollowup,
    Patient,
    ScheduledEvent,
    ScheduledEventStatus,
)
from app.db.repositories import lab_followups as lab_followups_repo
from app.db.repositories import scheduled_events as scheduled_events_repo

log = logging.getLogger(__name__)


LAB_EVENT_TYPE = "lab_followup_due"
STAGES: tuple[tuple[str, int], ...] = (
    ("d7", -7),
    ("d1", -1),
    ("overdue", 2),
)
DEFAULT_REMINDER_HOUR = 9
DEFAULT_TIMEZONE = "Asia/Kolkata"


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except Exception:
        return ZoneInfo(DEFAULT_TIMEZONE)


async def materialize_for_lab_followup(
    db: AsyncSession,
    lab: LabFollowup,
    *,
    patient_phone: str,
    timezone_name: str | None = None,
    now: datetime | None = None,
) -> list[ScheduledEvent]:
    """Enqueue lab_followup_due ScheduledEvents at any stage whose target
    time falls in the future. Idempotent — re-running for the same
    ``lab_followup_id`` creates no duplicates."""
    if lab.due_by is None:
        return []

    when_now = _ensure_utc(now or datetime.now(timezone.utc))
    tz = _tz(timezone_name)

    existing = await _list_pending_for_lab(db, lab_followup_id=lab.id)
    seen_stages = {(e.payload or {}).get("stage") for e in existing}

    out: list[ScheduledEvent] = []
    for stage, offset_days in STAGES:
        if stage in seen_stages:
            continue
        target_local_date = lab.due_by + timedelta(days=offset_days)
        target_local = datetime.combine(
            target_local_date, time(DEFAULT_REMINDER_HOUR, 0), tzinfo=tz
        )
        target_utc = target_local.astimezone(timezone.utc)
        if target_utc <= when_now:
            continue
        row = await scheduled_events_repo.enqueue(
            db,
            event_type=LAB_EVENT_TYPE,
            patient_id=patient_phone,
            payload={
                "lab_followup_id": lab.id,
                "patient_db_id": lab.patient_id,
                "test_name": lab.test_name,
                "due_by_iso": lab.due_by.isoformat(),
                "stage": stage,
            },
            scheduled_for=target_utc,
        )
        out.append(row)
    return out


async def cancel_for_lab_followup(
    db: AsyncSession,
    *,
    lab_followup_id: int,
    reason: str = "lab_completed",
) -> int:
    """Mark every pending lab_followup_due event for this followup as
    skipped. Called when the patient confirms completion or the row is
    deleted."""
    rows = await _list_pending_for_lab(db, lab_followup_id=lab_followup_id)
    cancelled = 0
    for row in rows:
        row.status = ScheduledEventStatus.skipped
        row.error = reason[:1000]
        cancelled += 1
    if cancelled:
        await db.flush()
    return cancelled


async def _list_pending_for_lab(
    db: AsyncSession, *, lab_followup_id: int
) -> list[ScheduledEvent]:
    stmt = (
        select(ScheduledEvent)
        .where(ScheduledEvent.event_type == LAB_EVENT_TYPE)
        .where(ScheduledEvent.status == ScheduledEventStatus.pending)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    return [
        r
        for r in rows
        if (r.payload or {}).get("lab_followup_id") == lab_followup_id
    ]


async def materialize_for_all_open(
    db: AsyncSession,
    *,
    today: date | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Background-loop entry point. Walks every open (due / booked) lab
    follow-up and materializes any new reminders."""
    today = today or datetime.now(timezone.utc).date()
    open_rows = await lab_followups_repo.list_open(db)
    new_count = 0
    skipped_no_phone = 0
    skipped_no_due = 0
    for lab in open_rows:
        if lab.due_by is None:
            skipped_no_due += 1
            continue
        patient = await db.get(Patient, lab.patient_id)
        if patient is None or not patient.phone:
            skipped_no_phone += 1
            continue
        created = await materialize_for_lab_followup(
            db, lab, patient_phone=patient.phone, now=now
        )
        new_count += len(created)
    return {
        "lab_followups_examined": len(open_rows),
        "skipped_no_phone": skipped_no_phone,
        "skipped_no_due": skipped_no_due,
        "new_lab_followup_events": new_count,
    }

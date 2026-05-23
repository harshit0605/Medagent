"""Materialize / cancel scheduled dose reminders for medication regimens.

For each active regimen the scheduler periodically computes the next dose
occurrences in a rolling window (default 48h). Each new occurrence becomes:

  1. an ``AdherenceEvent`` row (status=``scheduled``) — the durable record of
     "this dose is due at T", which the patient's tap (or the missed-dose
     sweep) later transitions to ``taken`` / ``skipped`` / ``missed``.
  2. a ``ScheduledEvent`` row (event_type=``dose_due``) carrying the
     ``adherence_event_id`` in its payload — picked up by the scheduler poll
     loop and dispatched as a WhatsApp message.

Idempotency is guaranteed two ways:
  - Python-side dedupe via :func:`adherence_events_repo.get_by_regimen_at`
  - DB-side unique constraint on (regimen_id, scheduled_at) — see migration
    20260502_0008.

Schedule format (only ``times_of_day`` supported initially; extend later):
    {
        "type": "times_of_day",
        "times": ["08:00", "20:00"],
        "timezone": "Asia/Kolkata",
        "frequency": "daily"
    }
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AdherenceStatus,
    Patient,
    Regimen,
    ScheduledEvent,
    ScheduledEventStatus,
)
from app.db.repositories import adherence_events as adherence_events_repo
from app.db.repositories import scheduled_events as scheduled_events_repo

log = logging.getLogger(__name__)


DOSE_EVENT_TYPE = "dose_due"
DEFAULT_WINDOW = timedelta(hours=48)
DEFAULT_TIMEZONE = "Asia/Kolkata"


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def compute_dose_occurrences(
    schedule: dict[str, Any],
    *,
    window_start: datetime,
    window_end: datetime,
) -> list[datetime]:
    """Return UTC datetimes when this regimen would fire inside the window.

    ``window_start`` is inclusive, ``window_end`` is exclusive. Both must be
    timezone-aware UTC. Unknown / malformed schedules return ``[]``.
    """
    schedule = schedule or {}
    sched_type = schedule.get("type", "times_of_day")
    if sched_type != "times_of_day":
        log.warning("unsupported schedule type: %s", sched_type)
        return []

    times_raw = schedule.get("times") or []
    tz_name = schedule.get("timezone") or DEFAULT_TIMEZONE
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        log.warning("unknown timezone %r — falling back to %s", tz_name, DEFAULT_TIMEZONE)
        tz = ZoneInfo(DEFAULT_TIMEZONE)

    parsed_times: list[time] = []
    for t in times_raw:
        try:
            hh, mm = str(t).split(":")[:2]
            parsed_times.append(time(int(hh), int(mm)))
        except (ValueError, AttributeError):
            log.warning("skipping malformed time entry %r", t)
            continue
    if not parsed_times:
        return []

    window_start_utc = _ensure_utc(window_start)
    window_end_utc = _ensure_utc(window_end)
    window_start_local = window_start_utc.astimezone(tz)
    window_end_local = window_end_utc.astimezone(tz)

    # Walk each calendar day intersecting the window in the regimen's tz, and
    # for each `times[]` entry emit the matching local datetime — converted
    # back to UTC for storage. One day before window_start covers a tz that
    # crosses midnight UTC; one day past covers the upper end.
    out: list[datetime] = []
    cursor = window_start_local.date() - timedelta(days=1)
    end_date = window_end_local.date() + timedelta(days=1)
    while cursor <= end_date:
        for t in parsed_times:
            local_dt = datetime.combine(cursor, t, tzinfo=tz)
            utc_dt = local_dt.astimezone(timezone.utc)
            if window_start_utc <= utc_dt < window_end_utc:
                out.append(utc_dt)
        cursor += timedelta(days=1)
    return sorted(out)


async def materialize_for_regimen(
    db: AsyncSession,
    regimen: Regimen,
    *,
    patient_phone: str,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> list[ScheduledEvent]:
    """Create AdherenceEvent + ScheduledEvent rows for this regimen's
    upcoming doses inside the window. Idempotent — re-running is a no-op
    for already-materialized occurrences. Returns the NEW ScheduledEvents.

    ``patient_phone`` is the WhatsApp wa_id used as ``ScheduledEvent.patient_id``
    (string). The dispatcher uses it as the recipient when sending."""
    now = datetime.now(timezone.utc)
    start = _ensure_utc(window_start or now)
    end = _ensure_utc(window_end or now + DEFAULT_WINDOW)

    # Clamp to regimen lifecycle.
    if regimen.starts_on is not None:
        starts_at = datetime.combine(
            regimen.starts_on, time(0, 0), tzinfo=timezone.utc
        )
        if start < starts_at:
            start = starts_at
    if regimen.ends_on is not None:
        ends_at = datetime.combine(
            regimen.ends_on, time(23, 59, 59), tzinfo=timezone.utc
        )
        if end > ends_at:
            end = ends_at
    if start >= end:
        return []

    occurrences = compute_dose_occurrences(
        regimen.schedule or {}, window_start=start, window_end=end
    )
    new_events: list[ScheduledEvent] = []
    for occurrence in occurrences:
        # Dedupe (Python-side) then defer to the DB unique constraint as a
        # safety net for races.
        existing = await adherence_events_repo.get_by_regimen_at(
            db, regimen_id=regimen.id, scheduled_at=occurrence
        )
        if existing is not None:
            continue
        adherence = await adherence_events_repo.create_scheduled(
            db,
            patient_id=regimen.patient_id,
            regimen_id=regimen.id,
            scheduled_at=occurrence,
        )
        scheduled = await scheduled_events_repo.enqueue(
            db,
            event_type=DOSE_EVENT_TYPE,
            patient_id=patient_phone,
            payload={
                "adherence_event_id": adherence.id,
                "regimen_id": regimen.id,
                "patient_db_id": regimen.patient_id,
                "medication_name": regimen.medication_name,
                "dose": regimen.dose,
                "scheduled_at_iso": occurrence.isoformat(),
            },
            scheduled_for=occurrence,
        )
        new_events.append(scheduled)
    return new_events


async def cancel_for_regimen(
    db: AsyncSession,
    *,
    regimen_id: int,
    reason: str = "regimen_deactivated",
) -> int:
    """Mark every pending dose_due ScheduledEvent for this regimen as skipped
    AND mark its AdherenceEvent as skipped (since it'll never fire). Returns
    the number of cancelled scheduled events."""
    stmt = (
        select(ScheduledEvent)
        .where(ScheduledEvent.event_type == DOSE_EVENT_TYPE)
        .where(ScheduledEvent.status == ScheduledEventStatus.pending)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    cancelled = 0
    for row in rows:
        if (row.payload or {}).get("regimen_id") != regimen_id:
            continue
        row.status = ScheduledEventStatus.skipped
        row.error = reason[:1000]
        adherence_id = (row.payload or {}).get("adherence_event_id")
        if adherence_id:
            adh = await adherence_events_repo.get(db, int(adherence_id))
            if adh is not None and adh.status == AdherenceStatus.scheduled:
                adh.status = AdherenceStatus.skipped
        cancelled += 1
    if cancelled:
        await db.flush()
    return cancelled


async def materialize_for_all_active_regimens(
    db: AsyncSession,
    *,
    today: date | None = None,
    window_end: datetime | None = None,
) -> dict[str, int]:
    """Background-loop entry point. Walks every regimen active today and
    materializes its upcoming dose reminders. Returns counts for the log /
    /scheduler/tick response.
    """
    today = today or datetime.now(timezone.utc).date()
    window_end_utc = window_end or (datetime.now(timezone.utc) + DEFAULT_WINDOW)

    from app.db.repositories import regimens as regimens_repo

    active = await regimens_repo.list_active_today(db, today)
    new_count = 0
    skipped_no_phone = 0
    for regimen in active:
        patient = await db.get(Patient, regimen.patient_id)
        if patient is None or not patient.phone:
            skipped_no_phone += 1
            continue
        created = await materialize_for_regimen(
            db,
            regimen,
            patient_phone=patient.phone,
            window_end=window_end_utc,
        )
        new_count += len(created)
    return {
        "regimens_examined": len(active),
        "new_dose_events": new_count,
        "skipped_no_phone": skipped_no_phone,
    }

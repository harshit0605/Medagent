"""Materialize / cancel pregnancy timeline reminders.

For every active pregnancy this sweep computes gestational age from the LMP
and enqueues two kinds of one-shot reminders:

  - ``pregnancy_milestone_due`` — antenatal-care milestones (visits / labs /
    scans / supplements) at their target gestational week (09:00 local).
  - ``pregnancy_weekly_due`` — a gentle weekly check-in for the next couple of
    gestational weeks (rolling; later sweeps extend the horizon).

Idempotent, mirroring the lab-followup materializer: re-running enqueues no
duplicates because we dedupe against the pending events already queued for the
pregnancy (by milestone key / gestational week). When a pregnancy ends the
orchestrator calls :func:`cancel_for_pregnancy` to skip pending reminders.

The milestone schedule + gestational math live in
:mod:`services.orchestrator.pregnancy` (pure, unit-tested); this module is the
DB-facing scheduler glue.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Patient, ScheduledEvent, ScheduledEventStatus
from app.db.repositories import pregnancies as pregnancies_repo
from app.db.repositories import scheduled_events as scheduled_events_repo
from services.orchestrator import pregnancy as preg

log = logging.getLogger(__name__)


PREGNANCY_MILESTONE_EVENT_TYPE = "pregnancy_milestone_due"
PREGNANCY_WEEKLY_EVENT_TYPE = "pregnancy_weekly_due"
_OUR_EVENT_TYPES = (
    PREGNANCY_MILESTONE_EVENT_TYPE,
    PREGNANCY_WEEKLY_EVENT_TYPE,
)

DEFAULT_REMINDER_HOUR = 9
DEFAULT_TIMEZONE = "Asia/Kolkata"
# How many upcoming weekly check-ins to materialize per sweep. The sweep runs
# every DOSE_MATERIALIZE_SECONDS (10 min), so a 2-week rolling horizon is
# always kept full without ever scheduling the whole pregnancy at once.
WEEKLY_LOOKAHEAD = 2


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _tz(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except Exception:
        return ZoneInfo(DEFAULT_TIMEZONE)


def _target_utc(target_date, tz: ZoneInfo) -> datetime:
    """09:00 local on ``target_date``, expressed in UTC."""
    local = datetime.combine(
        target_date, time(DEFAULT_REMINDER_HOUR, 0), tzinfo=tz
    )
    return local.astimezone(timezone.utc)


async def _list_pending_for_pregnancy(
    db: AsyncSession, *, pregnancy_id: int
) -> list[ScheduledEvent]:
    # Filter the payload key in SQL (payload ->> 'pregnancy_id') instead of
    # fetching ALL pending pregnancy events and filtering in Python — the
    # latter is O(all pending) per pregnancy.
    stmt = (
        select(ScheduledEvent)
        .where(ScheduledEvent.event_type.in_(_OUR_EVENT_TYPES))
        .where(ScheduledEvent.status == ScheduledEventStatus.pending)
        .where(
            ScheduledEvent.payload["pregnancy_id"].as_string()
            == str(pregnancy_id)
        )
    )
    return list((await db.execute(stmt)).scalars().all())


async def cancel_for_pregnancy(
    db: AsyncSession,
    *,
    pregnancy_id: int,
    reason: str = "pregnancy_ended",
) -> int:
    """Mark every pending pregnancy reminder for this episode as skipped.
    Called when the pregnancy ends so no further milestones/check-ins fire."""
    rows = await _list_pending_for_pregnancy(db, pregnancy_id=pregnancy_id)
    cancelled = 0
    for row in rows:
        row.status = ScheduledEventStatus.skipped
        row.error = reason[:1000]
        cancelled += 1
    if cancelled:
        await db.flush()
    return cancelled


async def materialize_for_pregnancy(
    db: AsyncSession,
    pregnancy,
    *,
    patient_phone: str,
    timezone_name: str | None = None,
    now: datetime | None = None,
) -> list[ScheduledEvent]:
    """Enqueue any not-yet-scheduled milestone + weekly-check-in events whose
    target time is in the future. Idempotent per ``pregnancy.id``."""
    try:
        lmp, _edd = preg.resolve_lmp_edd(pregnancy.lmp_date, pregnancy.edd)
    except ValueError:
        return []  # no anchor date — nothing to compute

    when_now = _ensure_utc(now or datetime.now(timezone.utc))
    tz = _tz(timezone_name)
    on_local = when_now.astimezone(tz).date()

    existing = await _list_pending_for_pregnancy(db, pregnancy_id=pregnancy.id)
    seen_keys = {
        (e.payload or {}).get("milestone_key")
        for e in existing
        if e.event_type == PREGNANCY_MILESTONE_EVENT_TYPE
    }
    seen_weeks = {
        (e.payload or {}).get("ga_week")
        for e in existing
        if e.event_type == PREGNANCY_WEEKLY_EVENT_TYPE
    }

    out: list[ScheduledEvent] = []

    # --- milestones ---------------------------------------------------------
    for milestone, target_date in preg.milestone_dates(lmp):
        if milestone.key in seen_keys:
            continue
        target_utc = _target_utc(target_date, tz)
        if target_utc <= when_now:
            continue
        row = await scheduled_events_repo.enqueue_idempotent(
            db,
            event_type=PREGNANCY_MILESTONE_EVENT_TYPE,
            patient_id=patient_phone,
            payload={
                "pregnancy_id": pregnancy.id,
                "patient_db_id": pregnancy.patient_id,
                "milestone_key": milestone.key,
                "kind": milestone.kind,
                "title": milestone.title,
                "detail": milestone.detail,
                "ga_week": milestone.week,
                "target_date_iso": target_date.isoformat(),
            },
            idempotency_key=f"preg_milestone:{pregnancy.id}:{milestone.key}",
            scheduled_for=target_utc,
        )
        if row is not None:
            out.append(row)

    # --- weekly check-ins (rolling horizon) ---------------------------------
    for week, target_date in preg.next_weekly_checkins(
        lmp, on=on_local, count=WEEKLY_LOOKAHEAD
    ):
        if week in seen_weeks:
            continue
        target_utc = _target_utc(target_date, tz)
        if target_utc <= when_now:
            continue
        row = await scheduled_events_repo.enqueue_idempotent(
            db,
            event_type=PREGNANCY_WEEKLY_EVENT_TYPE,
            patient_id=patient_phone,
            payload={
                "pregnancy_id": pregnancy.id,
                "patient_db_id": pregnancy.patient_id,
                "ga_week": week,
                "focus": preg.weekly_focus(week),
                "target_date_iso": target_date.isoformat(),
            },
            idempotency_key=f"preg_weekly:{pregnancy.id}:{week}",
            scheduled_for=target_utc,
        )
        if row is not None:
            out.append(row)

    return out


async def materialize_for_all_active(
    db: AsyncSession, *, now: datetime | None = None
) -> dict[str, int]:
    """Background-loop entry point. Walks every active pregnancy and
    materializes any new milestone + weekly-check-in reminders."""
    active = await pregnancies_repo.list_active(db)
    new_milestones = 0
    new_weekly = 0
    skipped_no_phone = 0
    skipped_no_anchor = 0
    for pregnancy in active:
        if pregnancy.lmp_date is None and pregnancy.edd is None:
            skipped_no_anchor += 1
            continue
        patient = await db.get(Patient, pregnancy.patient_id)
        if patient is None or not patient.phone:
            skipped_no_phone += 1
            continue
        created = await materialize_for_pregnancy(
            db, pregnancy, patient_phone=patient.phone, now=now
        )
        new_milestones += sum(
            1 for e in created
            if e.event_type == PREGNANCY_MILESTONE_EVENT_TYPE
        )
        new_weekly += sum(
            1 for e in created
            if e.event_type == PREGNANCY_WEEKLY_EVENT_TYPE
        )
    return {
        "pregnancies_examined": len(active),
        "skipped_no_phone": skipped_no_phone,
        "skipped_no_anchor": skipped_no_anchor,
        "new_pregnancy_milestone_events": new_milestones,
        "new_pregnancy_weekly_events": new_weekly,
    }

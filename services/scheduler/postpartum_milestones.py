"""Materialize / cancel postpartum-timeline reminders.

For every pregnancy in its postpartum phase (``postpartum_active=true``) this
sweep walks the postpartum milestone schedule + a rolling weekly check-in
horizon and enqueues:

  - ``postpartum_milestone_due`` — discrete PP-care milestones (early visit,
    EPDS screens, 6-week visit + contraception, baby vaccine reminders).
  - ``postpartum_weekly_due`` — a gentle weekly PP check-in for the next
    couple of weeks (rolling; later sweeps extend the horizon).

Idempotent, mirroring the pregnancy / lab-followup materializers: re-running
enqueues no duplicates because we dedupe against pending events already queued
for this pregnancy (by milestone key / PP week). When a PP episode ends the
orchestrator calls :func:`cancel_for_pregnancy` to skip pending reminders.

The schedule + PP date math live in :mod:`services.orchestrator.postpartum`
(pure, unit-tested); this module is the DB-facing scheduler glue.
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
from services.orchestrator import postpartum as pp

log = logging.getLogger(__name__)


POSTPARTUM_MILESTONE_EVENT_TYPE = "postpartum_milestone_due"
POSTPARTUM_WEEKLY_EVENT_TYPE = "postpartum_weekly_due"
_OUR_EVENT_TYPES = (
    POSTPARTUM_MILESTONE_EVENT_TYPE,
    POSTPARTUM_WEEKLY_EVENT_TYPE,
)

DEFAULT_REMINDER_HOUR = 9
DEFAULT_TIMEZONE = "Asia/Kolkata"
# How many upcoming weekly check-ins to materialize per sweep. The sweep runs
# every DOSE_MATERIALIZE_SECONDS (10 min), so a 2-week rolling horizon stays
# topped up without ever scheduling the full PP window at once.
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
    # Filter on payload['pregnancy_id'] in SQL — same shape as the pregnancy
    # materializer (payload uses ``pregnancy_id`` for both phases so a single
    # row continues to anchor both pre and post events).
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
    reason: str = "postpartum_ended",
) -> int:
    """Mark every pending PP reminder for this episode as skipped. Called
    when PP ends (or the underlying pregnancy is being re-closed)."""
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
) -> list[dict]:
    """Enqueue any not-yet-scheduled PP milestone + weekly check-in events
    whose target time is in the future. Idempotent per ``pregnancy.id``.
    One bulk insert; returns the inserted event-spec dicts.

    Skips silently if the row isn't in postpartum (``postpartum_active`` is
    False or ``delivery_date`` is unset) — caller errors don't blow up the
    sweep."""
    if not pregnancy.postpartum_active or pregnancy.delivery_date is None:
        return []

    when_now = _ensure_utc(now or datetime.now(timezone.utc))
    tz = _tz(timezone_name)
    on_local = when_now.astimezone(tz).date()

    existing = await _list_pending_for_pregnancy(db, pregnancy_id=pregnancy.id)
    seen_keys = {
        (e.payload or {}).get("milestone_key")
        for e in existing
        if e.event_type == POSTPARTUM_MILESTONE_EVENT_TYPE
    }
    seen_weeks = {
        (e.payload or {}).get("pp_week")
        for e in existing
        if e.event_type == POSTPARTUM_WEEKLY_EVENT_TYPE
    }

    specs: list[dict] = []

    # --- milestones ---------------------------------------------------------
    for milestone, target_date in pp.milestone_dates(pregnancy.delivery_date):
        if milestone.key in seen_keys:
            continue
        target_utc = _target_utc(target_date, tz)
        if target_utc <= when_now:
            continue
        specs.append(
            {
                "event_type": POSTPARTUM_MILESTONE_EVENT_TYPE,
                "patient_id": patient_phone,
                "payload": {
                    "pregnancy_id": pregnancy.id,
                    "patient_db_id": pregnancy.patient_id,
                    "milestone_key": milestone.key,
                    "kind": milestone.kind,
                    "title": milestone.title,
                    "detail": milestone.detail,
                    "pp_day": milestone.day,
                    "target_date_iso": target_date.isoformat(),
                },
                "idempotency_key": (
                    f"pp_milestone:{pregnancy.id}:{milestone.key}"
                ),
                "scheduled_for": target_utc,
            }
        )

    # --- weekly check-ins (rolling horizon) ---------------------------------
    for week, target_date in pp.next_weekly_checkins(
        pregnancy.delivery_date, on=on_local, count=WEEKLY_LOOKAHEAD
    ):
        if week in seen_weeks:
            continue
        target_utc = _target_utc(target_date, tz)
        if target_utc <= when_now:
            continue
        specs.append(
            {
                "event_type": POSTPARTUM_WEEKLY_EVENT_TYPE,
                "patient_id": patient_phone,
                "payload": {
                    "pregnancy_id": pregnancy.id,
                    "patient_db_id": pregnancy.patient_id,
                    "pp_week": week,
                    "focus": pp.weekly_focus(week),
                    "target_date_iso": target_date.isoformat(),
                },
                "idempotency_key": f"pp_weekly:{pregnancy.id}:{week}",
                "scheduled_for": target_utc,
            }
        )

    inserted_keys = await scheduled_events_repo.bulk_enqueue_idempotent(
        db, specs
    )
    out = [s for s in specs if s["idempotency_key"] in inserted_keys]

    return out


async def materialize_for_all_active(
    db: AsyncSession, *, now: datetime | None = None
) -> dict[str, int]:
    """Background-loop entry point. Walks every postpartum-active pregnancy
    and materializes any new PP milestone + weekly-check-in reminders."""
    active = await pregnancies_repo.list_postpartum_active(db)
    new_milestones = 0
    new_weekly = 0
    skipped_no_phone = 0
    skipped_no_anchor = 0
    for pregnancy in active:
        if pregnancy.delivery_date is None:
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
            if e["event_type"] == POSTPARTUM_MILESTONE_EVENT_TYPE
        )
        new_weekly += sum(
            1 for e in created
            if e["event_type"] == POSTPARTUM_WEEKLY_EVENT_TYPE
        )
    return {
        "postpartum_examined": len(active),
        "skipped_no_phone": skipped_no_phone,
        "skipped_no_anchor": skipped_no_anchor,
        "new_postpartum_milestone_events": new_milestones,
        "new_postpartum_weekly_events": new_weekly,
    }

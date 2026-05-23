"""Materialize / cancel refill reminders for regimens with supply tracking.

Each cycle, a regimen with both ``supply_days_initial`` and
``supply_started_on`` set has a computed expiry date::

    supply_runs_out = supply_started_on + supply_days_initial

The scheduler periodically materializes ``refill_due`` ScheduledEvents at
three "stages" before that date — T-7d, T-3d, T-1d — so the patient gets
escalating prompts as their supply runs low.

Cycle isolation: each (regimen_id, supply_started_on) pair is one
"cycle". The materializer encodes ``cycle_key`` in the payload and dedupes
on (regimen_id, cycle_key, stage). When the patient taps "Refilled" the
orchestrator calls :func:`cancel_for_regimen` to skip pending refill
events; the next scheduler tick then materializes the new cycle.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Patient, Regimen, ScheduledEvent, ScheduledEventStatus
from app.db.repositories import scheduled_events as scheduled_events_repo

log = logging.getLogger(__name__)


REFILL_EVENT_TYPE = "refill_due"
# Stages = (label, days-before-expiry). T-0 ("0d") = the day supply runs
# out — last-chance reminder.
STAGES: tuple[tuple[str, int], ...] = (
    ("d7", 7),
    ("d3", 3),
    ("d1", 1),
    ("d0", 0),
)
DEFAULT_REMINDER_HOUR = 9  # 09:00 in the regimen's schedule timezone
DEFAULT_TIMEZONE = "Asia/Kolkata"


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def supply_runs_out(regimen: Regimen) -> date | None:
    """The date this regimen's current cycle's supply expires, or None if
    supply tracking isn't configured."""
    if regimen.supply_days_initial is None or regimen.supply_started_on is None:
        return None
    return regimen.supply_started_on + timedelta(days=regimen.supply_days_initial)


def _cycle_key(regimen: Regimen) -> str | None:
    """Stable identifier for the current refill cycle (changes whenever the
    patient taps Refilled and supply_started_on is reset). Used to dedupe
    refill_due events within one cycle."""
    if regimen.supply_started_on is None:
        return None
    return regimen.supply_started_on.isoformat()


async def materialize_for_regimen(
    db: AsyncSession,
    regimen: Regimen,
    *,
    patient_phone: str,
    now: datetime | None = None,
) -> list[ScheduledEvent]:
    """Enqueue a refill_due ScheduledEvent for any stage (T-7/T-3/T-1/T-0)
    whose target time falls inside (now, supply_runs_out_local_time]. Idempotent:
    re-running for the same cycle creates no duplicates."""
    expiry = supply_runs_out(regimen)
    if expiry is None:
        return []  # supply tracking not configured

    when_now = _ensure_utc(now or datetime.now(timezone.utc))

    tz_name = (regimen.schedule or {}).get("timezone") or DEFAULT_TIMEZONE
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tz_name)
    except Exception:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(DEFAULT_TIMEZONE)

    cycle_key = _cycle_key(regimen)
    if cycle_key is None:
        return []

    # Pull existing refill_due events for this regimen + cycle so we don't
    # create dupes. Cheap (a handful per cycle).
    existing = await _list_pending_refills_for_regimen(
        db, regimen_id=regimen.id, cycle_key=cycle_key
    )
    seen_stages = {(e.payload or {}).get("stage") for e in existing}

    out: list[ScheduledEvent] = []
    for stage, offset_days in STAGES:
        if stage in seen_stages:
            continue
        # Fire at 09:00 local time on (expiry - offset_days). For T-0 that's
        # 09:00 of the supply-runs-out day — patient's last-chance prompt.
        target_date = expiry - timedelta(days=offset_days)
        target_local = datetime.combine(
            target_date, time(DEFAULT_REMINDER_HOUR, 0), tzinfo=tz
        )
        target_utc = target_local.astimezone(timezone.utc)
        if target_utc <= when_now:
            continue  # past
        row = await scheduled_events_repo.enqueue(
            db,
            event_type=REFILL_EVENT_TYPE,
            patient_id=patient_phone,
            payload={
                "regimen_id": regimen.id,
                "patient_db_id": regimen.patient_id,
                "medication_name": regimen.medication_name,
                "dose": regimen.dose,
                "stage": stage,
                "days_left": offset_days,
                "supply_runs_out_iso": expiry.isoformat(),
                "cycle_key": cycle_key,
            },
            scheduled_for=target_utc,
        )
        out.append(row)
    return out


async def cancel_for_regimen(
    db: AsyncSession,
    *,
    regimen_id: int,
    reason: str = "refill_done",
    cycle_key: str | None = None,
) -> int:
    """Mark every pending refill_due ScheduledEvent for this regimen as
    skipped. Called when the patient confirms a refill (current cycle is
    over) or when the regimen is deactivated. When ``cycle_key`` is given,
    only cancel events for that cycle — useful for a partial reset."""
    rows = await _list_pending_refills_for_regimen(
        db, regimen_id=regimen_id, cycle_key=cycle_key
    )
    cancelled = 0
    for row in rows:
        row.status = ScheduledEventStatus.skipped
        row.error = reason[:1000]
        cancelled += 1
    if cancelled:
        await db.flush()
    return cancelled


async def count_snoozes_for_cycle(
    db: AsyncSession,
    *,
    regimen_id: int,
    cycle_key: str,
) -> int:
    """Count how many snooze re-prompts the patient has stacked up for the
    current refill cycle. Used by the orchestrator's snooze cap — beyond
    a threshold we open a help ticket rather than keep deferring."""
    stmt = (
        select(ScheduledEvent)
        .where(ScheduledEvent.event_type == REFILL_EVENT_TYPE)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    return sum(
        1
        for r in rows
        if (r.payload or {}).get("regimen_id") == regimen_id
        and (r.payload or {}).get("cycle_key") == cycle_key
        and (r.payload or {}).get("stage") == "snooze1d"
    )


async def _list_pending_refills_for_regimen(
    db: AsyncSession,
    *,
    regimen_id: int,
    cycle_key: str | None = None,
) -> list[ScheduledEvent]:
    stmt = (
        select(ScheduledEvent)
        .where(ScheduledEvent.event_type == REFILL_EVENT_TYPE)
        .where(ScheduledEvent.status == ScheduledEventStatus.pending)
    )
    rows = list((await db.execute(stmt)).scalars().all())
    out: list[ScheduledEvent] = []
    for row in rows:
        payload = row.payload or {}
        if payload.get("regimen_id") != regimen_id:
            continue
        if cycle_key is not None and payload.get("cycle_key") != cycle_key:
            continue
        out.append(row)
    return out


async def materialize_for_all_active_regimens(
    db: AsyncSession,
    *,
    today: date | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Background-loop entry point. Walks every regimen active today with
    supply tracking configured and materializes any new refill events."""
    today = today or datetime.now(timezone.utc).date()

    from app.db.repositories import regimens as regimens_repo

    active = await regimens_repo.list_active_today(db, today)
    new_count = 0
    skipped_no_supply = 0
    skipped_no_phone = 0
    for regimen in active:
        if (
            regimen.supply_days_initial is None
            or regimen.supply_started_on is None
        ):
            skipped_no_supply += 1
            continue
        patient = await db.get(Patient, regimen.patient_id)
        if patient is None or not patient.phone:
            skipped_no_phone += 1
            continue
        created = await materialize_for_regimen(
            db, regimen, patient_phone=patient.phone, now=now
        )
        new_count += len(created)
    return {
        "regimens_examined": len(active),
        "skipped_no_supply": skipped_no_supply,
        "skipped_no_phone": skipped_no_phone,
        "new_refill_events": new_count,
    }

"""Materialize / cancel post-op checklist reminders.

For each active post-op episode, enqueue ``post_op_check_due`` events for the
day-N checklist items whose target time is still in the future. Idempotent —
dedupes against pending events by checklist key (mirrors the pregnancy
milestone materializer). Rides the dose-materialize loop.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Patient, ScheduledEvent, ScheduledEventStatus
from app.db.repositories import post_op as post_op_repo
from app.db.repositories import scheduled_events as scheduled_events_repo
from services.orchestrator import post_op as post_op_math

log = logging.getLogger(__name__)

POST_OP_CHECK_EVENT_TYPE = "post_op_check_due"
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


def _target_utc(target_date, tz: ZoneInfo) -> datetime:
    local = datetime.combine(
        target_date, time(DEFAULT_REMINDER_HOUR, 0), tzinfo=tz
    )
    return local.astimezone(timezone.utc)


async def _list_pending_for_episode(
    db: AsyncSession, *, episode_id: int
) -> list[ScheduledEvent]:
    # Filter the payload key in SQL rather than fetching all pending post-op
    # events and filtering in Python.
    stmt = (
        select(ScheduledEvent)
        .where(ScheduledEvent.event_type == POST_OP_CHECK_EVENT_TYPE)
        .where(ScheduledEvent.status == ScheduledEventStatus.pending)
        .where(
            ScheduledEvent.payload["episode_id"].as_string() == str(episode_id)
        )
    )
    return list((await db.execute(stmt)).scalars().all())


async def cancel_for_episode(
    db: AsyncSession, *, episode_id: int, reason: str = "post_op_ended"
) -> int:
    rows = await _list_pending_for_episode(db, episode_id=episode_id)
    cancelled = 0
    for row in rows:
        row.status = ScheduledEventStatus.skipped
        row.error = reason[:1000]
        cancelled += 1
    if cancelled:
        await db.flush()
    return cancelled


async def materialize_for_episode(
    db: AsyncSession,
    episode,
    *,
    patient_phone: str,
    timezone_name: str | None = None,
    now: datetime | None = None,
) -> list[ScheduledEvent]:
    when_now = _ensure_utc(now or datetime.now(timezone.utc))
    tz = _tz(timezone_name)

    existing = await _list_pending_for_episode(db, episode_id=episode.id)
    seen = {(e.payload or {}).get("check_key") for e in existing}

    out: list[ScheduledEvent] = []
    for check, target_date in post_op_math.check_dates(episode.surgery_date):
        if check.key in seen:
            continue
        target_utc = _target_utc(target_date, tz)
        if target_utc <= when_now:
            continue
        row = await scheduled_events_repo.enqueue(
            db,
            event_type=POST_OP_CHECK_EVENT_TYPE,
            patient_id=patient_phone,
            payload={
                "episode_id": episode.id,
                "patient_db_id": episode.patient_id,
                "check_key": check.key,
                "title": check.title,
                "detail": check.detail,
                "post_op_day": check.day,
                "procedure_name": episode.procedure_name,
                "target_date_iso": target_date.isoformat(),
            },
            scheduled_for=target_utc,
        )
        out.append(row)
    return out


async def materialize_for_all_active(
    db: AsyncSession, *, now: datetime | None = None
) -> dict[str, int]:
    active = await post_op_repo.list_active(db)
    new_checks = 0
    skipped_no_phone = 0
    for episode in active:
        patient = await db.get(Patient, episode.patient_id)
        if patient is None or not patient.phone:
            skipped_no_phone += 1
            continue
        created = await materialize_for_episode(
            db, episode, patient_phone=patient.phone, now=now
        )
        new_checks += len(created)
    return {
        "post_op_episodes_examined": len(active),
        "skipped_no_phone": skipped_no_phone,
        "new_post_op_check_events": new_checks,
    }

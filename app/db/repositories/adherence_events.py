"""Persistence for medication adherence events.

One row per scheduled dose occurrence. Lifecycle:
    scheduled → taken | skipped | missed | delayed

The materializer creates one ``AdherenceEvent`` (status=scheduled) per dose
occurrence within the scheduling window. The dispatcher fires a WhatsApp
reminder; the patient's tap (or its absence past the grace window) updates
the row's status.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, asc, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AdherenceEvent, AdherenceStatus


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def create_scheduled(
    session: AsyncSession,
    *,
    patient_id: int,
    regimen_id: int,
    scheduled_at: datetime,
) -> AdherenceEvent:
    row = AdherenceEvent(
        patient_id=patient_id,
        regimen_id=regimen_id,
        scheduled_at=_ensure_utc(scheduled_at),
        status=AdherenceStatus.scheduled,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def get(
    session: AsyncSession, adherence_event_id: int
) -> AdherenceEvent | None:
    return await session.get(AdherenceEvent, adherence_event_id)


async def get_by_regimen_at(
    session: AsyncSession, *, regimen_id: int, scheduled_at: datetime
) -> AdherenceEvent | None:
    """Lookup used by the materializer to dedupe before insert (idempotency).
    Will be backed by a unique index in migration 0008."""
    stmt = select(AdherenceEvent).where(
        AdherenceEvent.regimen_id == regimen_id,
        AdherenceEvent.scheduled_at == _ensure_utc(scheduled_at),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_for_patient(
    session: AsyncSession,
    patient_id: int,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 100,
) -> list[AdherenceEvent]:
    stmt = (
        select(AdherenceEvent)
        .where(AdherenceEvent.patient_id == patient_id)
        .order_by(asc(AdherenceEvent.scheduled_at))
        .limit(limit)
    )
    if since is not None:
        stmt = stmt.where(AdherenceEvent.scheduled_at >= _ensure_utc(since))
    if until is not None:
        stmt = stmt.where(AdherenceEvent.scheduled_at <= _ensure_utc(until))
    return list((await session.execute(stmt)).scalars().all())


async def list_recent_for_regimen(
    session: AsyncSession,
    regimen_id: int,
    *,
    limit: int = 5,
    up_to: datetime | None = None,
) -> list[AdherenceEvent]:
    """Most recent N adherence events for a regimen, newest first.

    ``up_to`` filters out future occurrences — important for escalation
    semantics ("the last N doses the patient was actually due to handle"),
    since the materializer pre-creates rows for future scheduled doses
    that would otherwise dominate this list and drown out real misses."""
    stmt = (
        select(AdherenceEvent)
        .where(AdherenceEvent.regimen_id == regimen_id)
        .order_by(desc(AdherenceEvent.scheduled_at))
        .limit(limit)
    )
    if up_to is not None:
        stmt = stmt.where(AdherenceEvent.scheduled_at <= _ensure_utc(up_to))
    return list((await session.execute(stmt)).scalars().all())


async def list_pending_past(
    session: AsyncSession,
    *,
    older_than: datetime,
    limit: int = 200,
) -> list[AdherenceEvent]:
    """Adherence events whose scheduled time is past `older_than` but still
    in `scheduled` status — candidates for the missed-dose sweep."""
    stmt = (
        select(AdherenceEvent)
        .where(
            and_(
                AdherenceEvent.status == AdherenceStatus.scheduled,
                AdherenceEvent.scheduled_at <= _ensure_utc(older_than),
            )
        )
        .order_by(asc(AdherenceEvent.scheduled_at))
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def update_status(
    session: AsyncSession,
    adherence_event_id: int,
    *,
    status: AdherenceStatus,
    confirmed_at: datetime | None = None,
    metadata: dict[str, Any] | None = None,
    channel_message_id: str | None = None,
) -> AdherenceEvent | None:
    row = await session.get(AdherenceEvent, adherence_event_id)
    if row is None:
        return None
    row.status = status
    if confirmed_at is not None:
        row.confirmed_at = _ensure_utc(confirmed_at)
    if metadata is not None:
        merged = dict(row.confirmation_metadata or {})
        merged.update(metadata)
        row.confirmation_metadata = merged
    if channel_message_id is not None:
        row.channel_message_id = channel_message_id
    await session.flush()
    await session.refresh(row)
    return row


async def mark_taken(
    session: AsyncSession,
    adherence_event_id: int,
    *,
    at: datetime | None = None,
    channel_message_id: str | None = None,
) -> AdherenceEvent | None:
    return await update_status(
        session,
        adherence_event_id,
        status=AdherenceStatus.taken,
        confirmed_at=at or datetime.now(timezone.utc),
        channel_message_id=channel_message_id,
    )


async def mark_skipped(
    session: AsyncSession,
    adherence_event_id: int,
    *,
    at: datetime | None = None,
    reason: str | None = None,
) -> AdherenceEvent | None:
    metadata = {"skipped_reason": reason} if reason else None
    return await update_status(
        session,
        adherence_event_id,
        status=AdherenceStatus.skipped,
        confirmed_at=at or datetime.now(timezone.utc),
        metadata=metadata,
    )


async def mark_missed(
    session: AsyncSession,
    adherence_event_id: int,
    *,
    at: datetime | None = None,
) -> AdherenceEvent | None:
    return await update_status(
        session,
        adherence_event_id,
        status=AdherenceStatus.missed,
        confirmed_at=at or datetime.now(timezone.utc),
    )


async def mark_delayed(
    session: AsyncSession,
    adherence_event_id: int,
    *,
    snoozed_until: datetime,
) -> AdherenceEvent | None:
    """Patient asked to snooze. Track that we re-scheduled (pointer kept in
    metadata) so the adherence rate calculation can still see the original
    scheduled_at."""
    return await update_status(
        session,
        adherence_event_id,
        status=AdherenceStatus.delayed,
        metadata={"snoozed_until": _ensure_utc(snoozed_until).isoformat()},
    )

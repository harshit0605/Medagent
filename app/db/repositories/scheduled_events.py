from __future__ import annotations

import os
import random
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import asc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ScheduledEvent, ScheduledEventStatus


# ---- Retry policy --------------------------------------------------------

# Exponential backoff schedule applied to transient dispatch
# failures. Index is ``attempt_count - 1`` (after the FIRST failure
# we wait one minute, after the second five, etc.). Past the end
# of this list the event is considered DLQ-ed and ``next_retry_at``
# stays NULL — ops decides whether to manually retry.
#
# Total span before DLQ: 1 + 5 + 15 + 60 + 240 = ~5h 21m. That's
# enough to ride out a Meta API outage of typical duration without
# burning patience on unfixable rows.
_RETRY_BACKOFF_MINUTES: tuple[int, ...] = (1, 5, 15, 60, 240)


def _max_attempts() -> int:
    """How many total dispatch attempts before giving up. Default
    matches the schedule length above. Override via env to make
    a deploy more or less tolerant of transient failures."""
    try:
        return int(
            os.getenv(
                "SCHEDULED_EVENT_MAX_ATTEMPTS",
                str(len(_RETRY_BACKOFF_MINUTES) + 1),
            )
        )
    except ValueError:
        return len(_RETRY_BACKOFF_MINUTES) + 1


def _compute_next_retry(
    *, attempt_count: int, now: datetime | None = None
) -> datetime | None:
    """Decide when (if ever) to retry after the ``attempt_count``-th
    failure.

    ``attempt_count`` is the count INCLUDING the just-failed
    attempt. After the 1st failure we use schedule[0]; after the
    last allowed failure we return None to indicate DLQ.

    Adds ±20% jitter so a fleet of co-failed events doesn't
    thunderstorm the dispatcher at the same retry instant.
    """
    if attempt_count >= _max_attempts():
        return None
    schedule_idx = attempt_count - 1
    if schedule_idx < 0 or schedule_idx >= len(_RETRY_BACKOFF_MINUTES):
        return None
    base_minutes = _RETRY_BACKOFF_MINUTES[schedule_idx]
    jitter = random.uniform(0.8, 1.2)
    delay = timedelta(minutes=base_minutes * jitter)
    return (now or datetime.now(timezone.utc)) + delay


# ---- Repo helpers --------------------------------------------------------


async def enqueue(
    session: AsyncSession,
    *,
    event_type: str,
    patient_id: str,
    payload: dict[str, Any],
    scheduled_for: datetime | None = None,
) -> ScheduledEvent:
    when = scheduled_for or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    row = ScheduledEvent(
        event_type=event_type,
        patient_id=patient_id,
        payload=payload,
        scheduled_for=when,
        status=ScheduledEventStatus.pending,
    )
    session.add(row)
    await session.flush()
    return row


async def enqueue_idempotent(
    session: AsyncSession,
    *,
    event_type: str,
    patient_id: str,
    payload: dict[str, Any],
    idempotency_key: str,
    scheduled_for: datetime | None = None,
) -> ScheduledEvent | None:
    """Enqueue a one-shot event keyed by ``idempotency_key``, race-safe.

    Uses INSERT ... ON CONFLICT DO NOTHING against the partial unique index on
    ``idempotency_key`` so a concurrent (or repeat) materialization of the same
    reminder inserts at most once. Returns the new row, or ``None`` when the
    key already existed (i.e. another tick/instance won the race or it was
    already materialized).
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    when = scheduled_for or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    stmt = (
        pg_insert(ScheduledEvent)
        .values(
            event_type=event_type,
            patient_id=patient_id,
            payload=payload,
            scheduled_for=when,
            status=ScheduledEventStatus.pending,
            idempotency_key=idempotency_key,
        )
        # index_where must match the PARTIAL unique index's predicate so
        # Postgres can infer it for the ON CONFLICT arbiter.
        .on_conflict_do_nothing(
            index_elements=["idempotency_key"],
            index_where=ScheduledEvent.idempotency_key.isnot(None),
        )
        .returning(ScheduledEvent.id)
    )
    new_id = (await session.execute(stmt)).scalar_one_or_none()
    if new_id is None:
        return None  # conflict — already materialized
    await session.flush()
    return await session.get(ScheduledEvent, new_id)


async def fetch_due(
    session: AsyncSession, *, now: datetime | None = None, limit: int = 50
) -> list[ScheduledEvent]:
    """Return events the dispatcher should attempt right now.

    Two cases combine into a single query:

        1. ``status=pending`` AND ``scheduled_for <= now``
           — never-dispatched events whose time has come.

        2. ``status=failed`` AND ``next_retry_at IS NOT NULL`` AND
           ``next_retry_at <= now``
           — previously-failed events whose backoff has elapsed.

    Failed events with ``next_retry_at IS NULL`` are dead-letter
    items — DO NOT pick them up automatically. Ops investigates
    them via the DLQ list endpoint and either manually retries
    (resetting next_retry_at) or marks them resolved.
    """
    when = now or datetime.now(timezone.utc)
    pending_due = (
        (ScheduledEvent.status == ScheduledEventStatus.pending)
        & (ScheduledEvent.scheduled_for <= when)
    )
    retry_due = (
        (ScheduledEvent.status == ScheduledEventStatus.failed)
        & (ScheduledEvent.next_retry_at.is_not(None))
        & (ScheduledEvent.next_retry_at <= when)
    )
    stmt = (
        select(ScheduledEvent)
        .where(or_(pending_due, retry_due))
        .order_by(asc(ScheduledEvent.scheduled_for))
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def mark_dispatched(
    session: AsyncSession, event_id: int, *, at: datetime | None = None
) -> ScheduledEvent | None:
    row = await session.get(ScheduledEvent, event_id)
    if row is None:
        return None
    row.status = ScheduledEventStatus.dispatched
    row.dispatched_at = at or datetime.now(timezone.utc)
    row.error = None
    # Successful dispatch clears any retry state — a future re-
    # enqueue of the same row would start a fresh policy cycle.
    row.next_retry_at = None
    await session.flush()
    return row


async def mark_failed(
    session: AsyncSession,
    event_id: int,
    *,
    error: str,
    now: datetime | None = None,
) -> ScheduledEvent | None:
    """Record a dispatch failure and decide whether to schedule a
    retry or send the row to the dead-letter queue.

    Returns the updated row. The caller should NOT need to know
    whether this was a retry or a DLQ — the row's ``next_retry_at``
    field is the authoritative answer:

        - non-null + future → will be retried automatically
        - null + status=failed → DLQ (attempts exhausted; ops only)
    """
    row = await session.get(ScheduledEvent, event_id)
    if row is None:
        return None
    when = now or datetime.now(timezone.utc)
    row.status = ScheduledEventStatus.failed
    row.error = error[:1000]
    row.attempt_count = (row.attempt_count or 0) + 1
    row.last_failed_at = when
    row.next_retry_at = _compute_next_retry(
        attempt_count=row.attempt_count, now=when
    )
    await session.flush()
    return row


async def mark_skipped(
    session: AsyncSession, event_id: int, *, reason: str | None = None
) -> ScheduledEvent | None:
    row = await session.get(ScheduledEvent, event_id)
    if row is None:
        return None
    row.status = ScheduledEventStatus.skipped
    if reason:
        row.error = reason[:1000]
    # Skipped is a terminal state — clear any pending retry.
    row.next_retry_at = None
    await session.flush()
    return row


async def list_recent(
    session: AsyncSession,
    *,
    status: str | None = None,
    patient_id: str | None = None,
    limit: int = 100,
) -> list[ScheduledEvent]:
    stmt = select(ScheduledEvent).order_by(ScheduledEvent.id.desc()).limit(limit)
    if status is not None:
        stmt = stmt.where(ScheduledEvent.status == ScheduledEventStatus(status))
    if patient_id is not None:
        stmt = stmt.where(ScheduledEvent.patient_id == patient_id)
    return list((await session.execute(stmt)).scalars().all())


# ---- DLQ surface ---------------------------------------------------------


async def list_dlq(
    session: AsyncSession, *, limit: int = 100
) -> list[ScheduledEvent]:
    """Dead-letter queue: failed events whose retry budget is
    exhausted (``next_retry_at IS NULL``). Ops reviews these
    manually — either re-queue with ``retry_dlq_event`` or
    mark them as won't-fix.
    """
    stmt = (
        select(ScheduledEvent)
        .where(ScheduledEvent.status == ScheduledEventStatus.failed)
        .where(ScheduledEvent.next_retry_at.is_(None))
        .order_by(ScheduledEvent.last_failed_at.desc().nulls_last())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def count_dlq(session: AsyncSession) -> int:
    """How many events are sitting in the DLQ. Drives the
    dashboard tile + service-health alert when non-zero."""
    stmt = (
        select(func.count())
        .select_from(ScheduledEvent)
        .where(ScheduledEvent.status == ScheduledEventStatus.failed)
        .where(ScheduledEvent.next_retry_at.is_(None))
    )
    return int((await session.execute(stmt)).scalar() or 0)


async def retry_dlq_event(
    session: AsyncSession,
    event_id: int,
    *,
    now: datetime | None = None,
) -> ScheduledEvent | None:
    """Manual ops re-queue: reset a DLQ item to pending so the
    dispatcher picks it up on the next tick. Resets the attempt
    counter so the row gets a fresh retry budget — the failure
    that DLQ-ed it might have been transient + the underlying
    cause already fixed."""
    row = await session.get(ScheduledEvent, event_id)
    if row is None:
        return None
    if row.status != ScheduledEventStatus.failed:
        return row  # idempotent: already not in DLQ
    row.status = ScheduledEventStatus.pending
    row.attempt_count = 0
    row.next_retry_at = None
    # Bump scheduled_for to now so the row is immediately eligible
    # under fetch_due's "scheduled_for <= now" predicate without
    # risking duplicate dispatch if the original time was minutes
    # in the future.
    row.scheduled_for = now or datetime.now(timezone.utc)
    await session.flush()
    return row

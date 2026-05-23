"""Persistence for lab follow-ups.

State machine::

    due → booked → completed → reviewed

The materializer (``services.scheduler.lab_followups``) emits
``lab_followup_due`` ScheduledEvents at T-7d, T-1d, and T+2d (overdue)
relative to ``due_by``. The orchestrator's ``lab_handler`` updates state
in response to button taps; clinicians close the loop by marking
``reviewed`` in the ops console.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import asc, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FollowupStatus, LabFollowup


async def create(
    session: AsyncSession,
    *,
    patient_id: int,
    test_name: str,
    due_by: date | None = None,
    notes: str | None = None,
) -> LabFollowup:
    row = LabFollowup(
        patient_id=patient_id,
        test_name=test_name,
        due_by=due_by,
        notes=notes,
        status=FollowupStatus.due,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def get(session: AsyncSession, lab_followup_id: int) -> LabFollowup | None:
    return await session.get(LabFollowup, lab_followup_id)


async def list_for_patient(
    session: AsyncSession, patient_id: int, *, limit: int = 50
) -> list[LabFollowup]:
    stmt = (
        select(LabFollowup)
        .where(LabFollowup.patient_id == patient_id)
        .order_by(desc(LabFollowup.created_at))
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_open(session: AsyncSession) -> list[LabFollowup]:
    """All lab follow-ups still requiring patient action — used by the
    materializer. Excludes completed / reviewed."""
    # ``ORDER BY due_by ASC NULLS LAST`` — order matters for Postgres.
    # ``asc(col.nulls_last())`` would emit ``NULLS LAST ASC`` which fails
    # to parse; use ``col.asc().nulls_last()`` to get the right order.
    stmt = (
        select(LabFollowup)
        .where(
            LabFollowup.status.in_(
                [FollowupStatus.due, FollowupStatus.booked]
            )
        )
        .order_by(LabFollowup.due_by.asc().nulls_last())
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_overdue(
    session: AsyncSession, *, today: date | None = None
) -> list[LabFollowup]:
    """Lab follow-ups still in due/booked state with due_by in the past."""
    today = today or date.today()
    stmt = select(LabFollowup).where(
        LabFollowup.status.in_(
            [FollowupStatus.due, FollowupStatus.booked]
        ),
        LabFollowup.due_by.isnot(None),
        LabFollowup.due_by < today,
    )
    return list((await session.execute(stmt)).scalars().all())


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def mark_booked(
    session: AsyncSession, lab_followup_id: int, *, at: datetime | None = None
) -> LabFollowup | None:
    row = await session.get(LabFollowup, lab_followup_id)
    if row is None:
        return None
    row.status = FollowupStatus.booked
    row.booked_at = _ensure_utc(at or datetime.now(timezone.utc))
    await session.flush()
    await session.refresh(row)
    return row


async def mark_completed(
    session: AsyncSession, lab_followup_id: int, *, at: datetime | None = None
) -> LabFollowup | None:
    row = await session.get(LabFollowup, lab_followup_id)
    if row is None:
        return None
    row.status = FollowupStatus.completed
    row.completed_at = _ensure_utc(at or datetime.now(timezone.utc))
    if row.booked_at is None:
        # Patient may have skipped Booked and gone straight to Completed —
        # stamp booked_at too for a clean audit trail.
        row.booked_at = row.completed_at
    await session.flush()
    await session.refresh(row)
    return row


async def mark_reviewed(
    session: AsyncSession, lab_followup_id: int, *, at: datetime | None = None
) -> LabFollowup | None:
    row = await session.get(LabFollowup, lab_followup_id)
    if row is None:
        return None
    row.status = FollowupStatus.reviewed
    row.reviewed_at = _ensure_utc(at or datetime.now(timezone.utc))
    if row.completed_at is None:
        row.completed_at = row.reviewed_at
    await session.flush()
    await session.refresh(row)
    return row

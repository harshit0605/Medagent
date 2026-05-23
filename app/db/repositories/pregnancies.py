"""Pregnancy episode persistence.

Thin repo behind the pregnancy timeline engine. A pregnancy is a bounded
episode (active → ended); the milestone materializer walks ``list_active``
exactly like the lab-followup materializer walks open follow-ups.

The DB enforces at most one ``active`` row per patient via a partial unique
index (migration 20260510_0038); ``create`` defends the same invariant at the
application layer with a friendly error so callers don't see a raw
IntegrityError.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Pregnancy, PregnancyStatus

ACTIVE = PregnancyStatus.active.value
ENDED = PregnancyStatus.ended.value


async def get(session: AsyncSession, pregnancy_id: int) -> Pregnancy | None:
    return await session.get(Pregnancy, pregnancy_id)


async def get_active_for_patient(
    session: AsyncSession, patient_id: int
) -> Pregnancy | None:
    """The patient's current active pregnancy, or ``None``."""
    stmt = (
        select(Pregnancy)
        .where(Pregnancy.patient_id == patient_id)
        .where(Pregnancy.status == ACTIVE)
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def list_active(session: AsyncSession) -> list[Pregnancy]:
    """All active pregnancies (sweep entry point)."""
    stmt = select(Pregnancy).where(Pregnancy.status == ACTIVE)
    return list((await session.execute(stmt)).scalars().all())


async def create(
    session: AsyncSession,
    *,
    patient_id: int,
    lmp_date: date | None = None,
    edd: date | None = None,
    notes: str | None = None,
) -> Pregnancy:
    """Open a new active pregnancy. Requires at least one of ``lmp_date`` /
    ``edd`` (the engine derives the other). Raises ``ValueError`` if the
    patient already has an active pregnancy."""
    if lmp_date is None and edd is None:
        raise ValueError("at least one of lmp_date or edd is required")
    existing = await get_active_for_patient(session, patient_id)
    if existing is not None:
        raise ValueError(
            f"patient {patient_id} already has an active pregnancy "
            f"(id={existing.id}); end it before opening a new one"
        )
    row = Pregnancy(
        patient_id=patient_id,
        lmp_date=lmp_date,
        edd=edd,
        status=ACTIVE,
        notes=notes,
    )
    session.add(row)
    await session.flush()
    return row


async def end_pregnancy(
    session: AsyncSession,
    pregnancy_id: int,
    *,
    reason: str | None = None,
    at: datetime | None = None,
) -> Pregnancy | None:
    """Close a pregnancy episode (delivered / miscarried / corrected). The
    milestone sweep ignores ended rows, so this stops further reminders."""
    row = await session.get(Pregnancy, pregnancy_id)
    if row is None:
        return None
    row.status = ENDED
    row.ended_at = at or datetime.now(timezone.utc)
    if reason:
        row.ended_reason = reason[:255]
    await session.flush()
    return row

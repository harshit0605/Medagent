"""Post-op recovery episode persistence (mirrors pregnancies repo)."""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PostOpEpisode

ACTIVE = "active"
ENDED = "ended"


async def get(session: AsyncSession, episode_id: int) -> PostOpEpisode | None:
    return await session.get(PostOpEpisode, episode_id)


async def get_active_for_patient(
    session: AsyncSession, patient_id: int
) -> PostOpEpisode | None:
    stmt = (
        select(PostOpEpisode)
        .where(PostOpEpisode.patient_id == patient_id)
        .where(PostOpEpisode.status == ACTIVE)
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def list_active(session: AsyncSession) -> list[PostOpEpisode]:
    stmt = select(PostOpEpisode).where(PostOpEpisode.status == ACTIVE)
    return list((await session.execute(stmt)).scalars().all())


async def create(
    session: AsyncSession,
    *,
    patient_id: int,
    procedure_name: str,
    surgery_date: date,
    notes: str | None = None,
) -> PostOpEpisode:
    existing = await get_active_for_patient(session, patient_id)
    if existing is not None:
        raise ValueError(
            f"patient {patient_id} already has an active post-op episode "
            f"(id={existing.id}); end it first"
        )
    row = PostOpEpisode(
        patient_id=patient_id,
        procedure_name=procedure_name[:255],
        surgery_date=surgery_date,
        status=ACTIVE,
        notes=notes,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def end_episode(
    session: AsyncSession,
    episode_id: int,
    *,
    reason: str | None = None,
    at: datetime | None = None,
) -> PostOpEpisode | None:
    row = await session.get(PostOpEpisode, episode_id)
    if row is None:
        return None
    row.status = ENDED
    row.ended_at = at or datetime.now(timezone.utc)
    if reason:
        row.ended_reason = reason[:255]
    await session.flush()
    await session.refresh(row)
    return row

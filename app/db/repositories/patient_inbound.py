from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PatientInboundState


async def get_last_inbound(session: AsyncSession, patient_id: str) -> datetime | None:
    stmt = select(PatientInboundState.last_inbound_at).where(
        PatientInboundState.patient_id == patient_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def set_last_inbound(session: AsyncSession, patient_id: str, when: datetime) -> None:
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    else:
        when = when.astimezone(timezone.utc)

    stmt = insert(PatientInboundState).values(
        patient_id=patient_id,
        last_inbound_at=when,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[PatientInboundState.patient_id],
        set_={"last_inbound_at": when},
    )
    await session.execute(stmt)

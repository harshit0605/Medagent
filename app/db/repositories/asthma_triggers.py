"""Asthma trigger-diary persistence.

Thin repo for the trigger diary / voice trigger diary (SoT §3C). The patient
logs what set off their symptoms; the clinic reviews patterns over time.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AsthmaTriggerLog


async def create(
    session: AsyncSession,
    *,
    patient_id: int,
    trigger_text: str,
    source: str = "patient_self_report",
    logged_at: datetime | None = None,
) -> AsthmaTriggerLog:
    row = AsthmaTriggerLog(
        patient_id=patient_id,
        trigger_text=trigger_text[:255],
        source=source,
        logged_at=logged_at or datetime.now(timezone.utc),
    )
    session.add(row)
    await session.flush()
    return row


async def list_for_patient(
    session: AsyncSession, patient_id: int, *, limit: int = 50
) -> list[AsthmaTriggerLog]:
    stmt = (
        select(AsthmaTriggerLog)
        .where(AsthmaTriggerLog.patient_id == patient_id)
        .order_by(desc(AsthmaTriggerLog.logged_at))
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def count_since(
    session: AsyncSession, patient_id: int, *, since: datetime
) -> int:
    stmt = (
        select(AsthmaTriggerLog)
        .where(AsthmaTriggerLog.patient_id == patient_id)
        .where(AsthmaTriggerLog.logged_at >= since)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return len(rows)

"""Visit brief persistence.

Briefs are append-only — every generation writes a new row.
Helpers split into:

    create()         — write the row after the LLM call lands
    create_failed()  — record a failed-generation row so ops
                       can see attempts even when the LLM
                       errored out
    list_for_patient — patient-detail page widget
    get              — detail view + endpoint round-trip
    mark_sent        — first-time GET stamps draft → sent so
                       the audit log captures who saw it
    supersede_for_appointment — when a fresh brief lands for
                       an appointment, mark the prior briefs
                       superseded so the per-appointment
                       lookup is deterministic

We deliberately avoid an ``update_summary`` helper. Briefs are
treated as evidence — if the doctor wants a different summary,
they regenerate (creates a new row). Mutating an existing
brief would break the "what did the doctor see at the time?"
audit story.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import VisitBrief


async def create(
    session: AsyncSession,
    *,
    patient_id: int,
    appointment_id: int | None,
    doctor_id: int | None,
    window_start: datetime,
    window_end: datetime,
    llm_model: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    summary: str,
    talking_points: list[str],
    red_flags: list[str],
    key_metrics: dict[str, Any],
    generated_by: str | None,
) -> VisitBrief:
    row = VisitBrief(
        patient_id=patient_id,
        appointment_id=appointment_id,
        doctor_id=doctor_id,
        window_start=window_start,
        window_end=window_end,
        llm_model=llm_model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        summary=summary,
        talking_points=talking_points,
        red_flags=red_flags,
        key_metrics=key_metrics,
        status="draft",
        generated_by=generated_by,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def create_failed(
    session: AsyncSession,
    *,
    patient_id: int,
    appointment_id: int | None,
    doctor_id: int | None,
    window_start: datetime,
    window_end: datetime,
    llm_model: str,
    error: str,
    generated_by: str | None,
) -> VisitBrief:
    """Record a failed generation. We keep these rows so ops can
    see "we tried at 14:00 and the LLM 500'd" rather than the
    user clicking Generate and getting silent failure."""
    row = VisitBrief(
        patient_id=patient_id,
        appointment_id=appointment_id,
        doctor_id=doctor_id,
        window_start=window_start,
        window_end=window_end,
        llm_model=llm_model,
        summary="",
        talking_points=[],
        red_flags=[],
        key_metrics={},
        status="draft",
        generated_by=generated_by,
        error=error,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def get(
    session: AsyncSession, brief_id: int
) -> VisitBrief | None:
    return await session.get(VisitBrief, brief_id)


async def list_for_patient(
    session: AsyncSession,
    patient_id: int,
    *,
    limit: int = 10,
    include_failed: bool = False,
) -> list[VisitBrief]:
    """Newest-first list of briefs for a patient. Failed briefs
    (``error IS NOT NULL``) are excluded by default since the UI
    widget shows the most recent successful brief; ops admin
    views set ``include_failed=True``."""
    stmt = (
        select(VisitBrief)
        .where(VisitBrief.patient_id == patient_id)
        .order_by(VisitBrief.generated_at.desc())
        .limit(limit)
    )
    if not include_failed:
        stmt = stmt.where(VisitBrief.error.is_(None))
    return list((await session.execute(stmt)).scalars().all())


async def mark_sent(
    session: AsyncSession, brief_id: int
) -> VisitBrief | None:
    row = await session.get(VisitBrief, brief_id)
    if row is None or row.status != "draft":
        return row
    row.status = "sent"
    await session.flush()
    return row


async def supersede_for_appointment(
    session: AsyncSession,
    *,
    appointment_id: int,
    keep_brief_id: int,
) -> int:
    """Mark all briefs for ``appointment_id`` other than
    ``keep_brief_id`` as superseded. Returns the count of rows
    flipped — caller can log this for observability."""
    stmt = (
        update(VisitBrief)
        .where(VisitBrief.appointment_id == appointment_id)
        .where(VisitBrief.id != keep_brief_id)
        .where(VisitBrief.status != "superseded")
        .values(status="superseded")
    )
    result = await session.execute(stmt)
    return result.rowcount or 0

"""Scheduled-event dead-letter-queue endpoints. Extracted from main.py."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import scheduled_events as scheduled_events_repo
from app.db.session import get_session

router = APIRouter()


class ScheduledEventDLQRowDTO(BaseModel):
    """One scheduled-event in dead-letter state. Just enough fields
    for the ops UI to render a triage list — error + last_failed_at
    are the diagnostic pair, attempt_count tells ops how far it
    got before giving up."""

    id: int
    event_type: str
    patient_id: str
    scheduled_for: datetime
    last_failed_at: datetime | None
    attempt_count: int
    error: str | None
    payload: dict[str, Any]


class ScheduledEventDLQResponseDTO(BaseModel):
    rows: list[ScheduledEventDLQRowDTO]
    total: int


@router.get("/ops/dlq", response_model=ScheduledEventDLQResponseDTO)
async def list_dlq_endpoint(
    db: AsyncSession = Depends(get_session),
    limit: int = 100,
) -> ScheduledEventDLQResponseDTO:
    """Dead-letter queue for scheduled events. Items here have
    failed enough times to exhaust their retry budget and need
    manual ops attention. The retry endpoint
    (``POST /ops/dlq/{id}/retry``) re-queues a row with a fresh
    attempt counter — use after fixing the underlying issue
    (template approved, network restored, etc.)."""
    if limit <= 0 or limit > 500:
        raise HTTPException(
            status_code=400, detail="limit must be in [1, 500]"
        )
    rows = await scheduled_events_repo.list_dlq(db, limit=limit)
    total = await scheduled_events_repo.count_dlq(db)
    return ScheduledEventDLQResponseDTO(
        rows=[
            ScheduledEventDLQRowDTO(
                id=r.id,
                event_type=r.event_type,
                patient_id=r.patient_id,
                scheduled_for=r.scheduled_for,
                last_failed_at=r.last_failed_at,
                attempt_count=r.attempt_count or 0,
                error=r.error,
                payload=r.payload or {},
            )
            for r in rows
        ],
        total=total,
    )


@router.post("/ops/dlq/{event_id}/retry")
async def retry_dlq_endpoint(
    event_id: int,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Manual re-queue of a DLQ item. Resets attempt_count to 0
    and sets ``scheduled_for`` to now, so the dispatcher's next
    tick picks the row up. Idempotent on already-non-DLQ rows
    (returns the row unchanged)."""
    row = await scheduled_events_repo.retry_dlq_event(db, event_id)
    if row is None:
        raise HTTPException(
            status_code=404, detail="scheduled_event not found"
        )
    await db.commit()
    return {
        "id": row.id,
        "status": row.status.value,
        "attempt_count": row.attempt_count or 0,
        "scheduled_for": (
            row.scheduled_for.isoformat() if row.scheduled_for else None
        ),
    }

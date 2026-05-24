"""Audit-log search endpoint. Extracted from main.py."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import audit as audit_repo
from app.db.session import get_session

router = APIRouter()


class AuditRecordDTO(BaseModel):
    """Trimmed AuditRecord shape for the search UI. ``details`` is
    a free-form dict the frontend renders as-is — different record
    types stash different metadata in there and a strict schema
    would constrain future loggers."""

    id: int
    record_type: str
    patient_id: str
    outbound_mode: str | None = None
    flow_action: str | None = None
    reason_codes: list[str]
    details: dict[str, Any]
    logged_at: datetime


class AuditSearchResponseDTO(BaseModel):
    rows: list[AuditRecordDTO]
    total: int
    limit: int
    offset: int


def _parse_search_dt(value: str | None) -> datetime | None:
    """Parse a search-filter ISO datetime. Returns None for blank,
    raises HTTPException(400) on bad values so the UI surfaces a
    clear error rather than silently dropping the filter."""
    if value is None or value.strip() == "":
        return None
    try:
        # ``fromisoformat`` accepts ``2026-05-08`` (date-only) and
        # ``2026-05-08T12:00:00+00:00`` (full RFC). The UI sends
        # ``<input type="date">`` so date-only is the common case.
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"invalid datetime {value!r}: {exc}",
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@router.get("/ops/audit-search", response_model=AuditSearchResponseDTO)
async def audit_search(
    db: AsyncSession = Depends(get_session),
    patient_id: str | None = None,
    record_type: str | None = None,
    reason_code: str | None = None,
    flow_action: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> AuditSearchResponseDTO:
    """Filtered + paginated audit-records search. Powers the
    ops-console /audit-search page.

    Validation:
        ``limit`` clamped to [1, 200] — beyond 200 the JSON
        response gets unwieldy and the UI table needs pagination
        anyway. Negative offsets clamped to 0.
    """
    if limit <= 0 or limit > 200:
        raise HTTPException(
            status_code=400, detail="limit must be in [1, 200]"
        )
    if offset < 0:
        offset = 0

    rows, total = await audit_repo.search(
        db,
        patient_id=patient_id or None,
        record_type=record_type or None,
        reason_code=reason_code or None,
        flow_action=flow_action or None,
        since=_parse_search_dt(since),
        until=_parse_search_dt(until),
        limit=limit,
        offset=offset,
    )

    return AuditSearchResponseDTO(
        rows=[
            AuditRecordDTO(
                id=r.id,
                record_type=r.record_type,
                patient_id=r.patient_id,
                outbound_mode=r.outbound_mode,
                flow_action=r.flow_action,
                reason_codes=list(r.reason_codes or []),
                details=dict(r.details or {}),
                logged_at=r.logged_at,
            )
            for r in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )

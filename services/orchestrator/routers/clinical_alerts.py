"""Clinical-alert endpoints (triage-classifier patient-safety queue).

Extracted from main.py. Surfaces alerts created by the inbound triage
classifier for the ops-console queue: list/counts/detail + acknowledge /
resolve / manual page.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session

router = APIRouter()


class ClinicalAlertDTO(BaseModel):
    """Patient-safety alert created by the triage classifier
    on inbound messages. Surfaced in the ops-console alerts
    queue so a doctor can scan critical-first then ack/resolve."""

    id: int
    patient_id: int
    patient_phone: str | None
    message_id: int | None
    severity: str
    red_flags: list[str]
    clinical_summary: str
    inbound_text: str
    status: str
    llm_model: str | None
    created_at: datetime
    acknowledged_at: datetime | None
    acknowledged_by: str | None
    resolved_at: datetime | None
    resolved_by: str | None
    resolution_notes: str | None
    paged_doctor_id: int | None
    paged_at: datetime | None
    paged_attempts: int


def _clinical_alert_to_dto(row: Any) -> ClinicalAlertDTO:
    return ClinicalAlertDTO(
        id=row.id,
        patient_id=row.patient_id,
        patient_phone=row.patient_phone,
        message_id=row.message_id,
        severity=row.severity,
        red_flags=list(row.red_flags or []),
        clinical_summary=row.clinical_summary,
        inbound_text=row.inbound_text,
        status=row.status,
        llm_model=row.llm_model,
        created_at=row.created_at,
        acknowledged_at=row.acknowledged_at,
        acknowledged_by=row.acknowledged_by,
        resolved_at=row.resolved_at,
        resolved_by=row.resolved_by,
        resolution_notes=row.resolution_notes,
        paged_doctor_id=row.paged_doctor_id,
        paged_at=row.paged_at,
        paged_attempts=row.paged_attempts or 0,
    )


class ClinicalAlertActionRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=128)
    notes: str | None = Field(default=None, max_length=2000)


@router.get("/clinical-alerts", response_model=list[ClinicalAlertDTO])
async def list_clinical_alerts(
    status: str | None = Query(
        default=None,
        description=(
            "Filter by status (open / acknowledged / resolved). "
            "Omit to see everything newest-first."
        ),
    ),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_session),
) -> list[ClinicalAlertDTO]:
    from app.db.repositories import (
        clinical_alerts as clinical_alerts_repo,
    )

    if status is not None and status not in {
        "open",
        "acknowledged",
        "resolved",
    }:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unknown status {status!r}; "
                "valid: open / acknowledged / resolved"
            ),
        )
    rows = await clinical_alerts_repo.list_recent(
        db, status=status, limit=limit
    )
    return [_clinical_alert_to_dto(r) for r in rows]


@router.get(
    "/clinical-alerts/counts", response_model=dict[str, int]
)
async def clinical_alert_counts(
    db: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    """Tile counters for the alerts dashboard. Always returns
    every status key (zero-filled) so the UI doesn't have to
    test for presence."""
    from app.db.repositories import (
        clinical_alerts as clinical_alerts_repo,
    )

    raw = await clinical_alerts_repo.count_by_status(db)
    return {
        "open": raw.get("open", 0),
        "acknowledged": raw.get("acknowledged", 0),
        "resolved": raw.get("resolved", 0),
    }


@router.get(
    "/clinical-alerts/{alert_id}", response_model=ClinicalAlertDTO
)
async def get_clinical_alert(
    alert_id: int,
    db: AsyncSession = Depends(get_session),
) -> ClinicalAlertDTO:
    from app.db.repositories import (
        clinical_alerts as clinical_alerts_repo,
    )

    row = await clinical_alerts_repo.get(db, alert_id)
    if row is None:
        raise HTTPException(status_code=404, detail="alert not found")
    return _clinical_alert_to_dto(row)


@router.post(
    "/clinical-alerts/{alert_id}/acknowledge",
    response_model=ClinicalAlertDTO,
)
async def acknowledge_clinical_alert(
    alert_id: int,
    body: ClinicalAlertActionRequest,
    db: AsyncSession = Depends(get_session),
) -> ClinicalAlertDTO:
    from app.db.repositories import (
        clinical_alerts as clinical_alerts_repo,
    )

    row = await clinical_alerts_repo.acknowledge(
        db, alert_id, actor=body.actor
    )
    if row is None:
        raise HTTPException(status_code=404, detail="alert not found")
    await db.commit()
    return _clinical_alert_to_dto(row)


@router.post(
    "/clinical-alerts/{alert_id}/resolve",
    response_model=ClinicalAlertDTO,
)
async def resolve_clinical_alert(
    alert_id: int,
    body: ClinicalAlertActionRequest,
    db: AsyncSession = Depends(get_session),
) -> ClinicalAlertDTO:
    from app.db.repositories import (
        clinical_alerts as clinical_alerts_repo,
    )

    row = await clinical_alerts_repo.resolve(
        db, alert_id, actor=body.actor, notes=body.notes
    )
    if row is None:
        raise HTTPException(status_code=404, detail="alert not found")
    await db.commit()
    return _clinical_alert_to_dto(row)


@router.post(
    "/clinical-alerts/{alert_id}/page",
    response_model=ClinicalAlertDTO,
)
async def page_clinical_alert(
    alert_id: int,
    db: AsyncSession = Depends(get_session),
) -> ClinicalAlertDTO:
    """Manually trigger (or retrigger) a paging attempt for an
    alert. Returns the updated row so the UI can show the new
    paged_at + paged_attempts. Used when ops sees a stalled
    page and wants to force another attempt without waiting
    for the sweep, or to escalate before the 5-min interval."""
    from app.db.repositories import (
        clinical_alerts as clinical_alerts_repo,
    )
    from services.orchestrator import clinical_alert_pager

    result = await clinical_alert_pager.page_alert(
        db, alert_id=alert_id
    )
    if result.get("error") == "alert_not_found":
        raise HTTPException(status_code=404, detail="alert not found")
    await db.commit()

    row = await clinical_alerts_repo.get(db, alert_id)
    if row is None:
        raise HTTPException(status_code=404, detail="alert not found")
    return _clinical_alert_to_dto(row)


@router.get(
    "/patients/{patient_id}/clinical-alerts",
    response_model=list[ClinicalAlertDTO],
)
async def list_patient_clinical_alerts(
    patient_id: int,
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_session),
) -> list[ClinicalAlertDTO]:
    from app.db.repositories import (
        clinical_alerts as clinical_alerts_repo,
    )

    rows = await clinical_alerts_repo.list_for_patient(
        db, patient_id, limit=limit
    )
    return [_clinical_alert_to_dto(r) for r in rows]

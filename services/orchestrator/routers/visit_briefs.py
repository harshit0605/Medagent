"""Visit-brief endpoints (LLM pre-visit summaries).

Extracted from main.py. Generation delegates to
``services.orchestrator.visit_brief_generator``; these endpoints are the
ops-console on-demand surface + the list/detail views.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import patients as patients_repo
from app.db.session import get_session

router = APIRouter()


class VisitBriefDTO(BaseModel):
    """LLM-generated pre-visit summary. ``error`` is non-null
    on failed generations — UI should hide those by default."""

    id: int
    patient_id: int
    appointment_id: int | None
    doctor_id: int | None
    generated_at: datetime
    window_start: datetime
    window_end: datetime
    llm_model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    summary: str
    talking_points: list[str]
    red_flags: list[str]
    key_metrics: dict[str, Any]
    status: str
    generated_by: str | None
    error: str | None


def _visit_brief_to_dto(row: Any) -> VisitBriefDTO:
    return VisitBriefDTO(
        id=row.id,
        patient_id=row.patient_id,
        appointment_id=row.appointment_id,
        doctor_id=row.doctor_id,
        generated_at=row.generated_at,
        window_start=row.window_start,
        window_end=row.window_end,
        llm_model=row.llm_model,
        prompt_tokens=row.prompt_tokens,
        completion_tokens=row.completion_tokens,
        summary=row.summary,
        talking_points=list(row.talking_points or []),
        red_flags=list(row.red_flags or []),
        key_metrics=dict(row.key_metrics or {}),
        status=row.status,
        generated_by=row.generated_by,
        error=row.error,
    )


class GenerateVisitBriefRequest(BaseModel):
    appointment_id: int | None = None
    doctor_id: int | None = None
    window_days: int = Field(default=30, ge=1, le=180)
    generated_by: str | None = Field(default=None, max_length=128)


@router.post(
    "/patients/{patient_id}/visit-briefs/generate",
    response_model=VisitBriefDTO,
)
async def generate_patient_visit_brief(
    patient_id: int,
    body: GenerateVisitBriefRequest | None = None,
    db: AsyncSession = Depends(get_session),
) -> VisitBriefDTO:
    """Trigger a fresh visit-brief generation. Manual on-demand
    path — the eventual auto-generation flow (T-2h before
    appointment) reuses ``visit_brief_generator.generate_brief``
    via the dispatcher; this endpoint is for the ops console
    "Generate brief" button."""
    from services.orchestrator import visit_brief_generator

    payload = body or GenerateVisitBriefRequest()

    # Verify patient exists before doing the LLM round trip —
    # cheaper to 404 here than after burning tokens.
    patient = await patients_repo.get(db, patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="patient not found")

    try:
        brief = await visit_brief_generator.generate_brief(
            db,
            patient_id=patient_id,
            appointment_id=payload.appointment_id,
            doctor_id=payload.doctor_id,
            window_days=payload.window_days,
            generated_by=payload.generated_by,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        # Failed generation row already persisted by the
        # generator — surface a 502 so the caller can retry.
        raise HTTPException(status_code=502, detail=str(exc))

    await db.commit()
    return _visit_brief_to_dto(brief)


@router.get(
    "/patients/{patient_id}/visit-briefs",
    response_model=list[VisitBriefDTO],
)
async def list_patient_visit_briefs(
    patient_id: int,
    limit: int = Query(default=10, ge=1, le=50),
    include_failed: bool = Query(
        default=False,
        description=(
            "When true, includes briefs whose LLM call errored "
            "out — useful for ops debugging, hidden from doctor "
            "views by default."
        ),
    ),
    db: AsyncSession = Depends(get_session),
) -> list[VisitBriefDTO]:
    from app.db.repositories import visit_briefs as visit_briefs_repo

    rows = await visit_briefs_repo.list_for_patient(
        db,
        patient_id,
        limit=limit,
        include_failed=include_failed,
    )
    return [_visit_brief_to_dto(r) for r in rows]


@router.get("/visit-briefs/{brief_id}", response_model=VisitBriefDTO)
async def get_visit_brief(
    brief_id: int,
    db: AsyncSession = Depends(get_session),
) -> VisitBriefDTO:
    """Detail view. First successful GET on a draft brief flips
    its status to ``sent`` so the audit log captures who saw
    it."""
    from app.db.repositories import visit_briefs as visit_briefs_repo

    row = await visit_briefs_repo.get(db, brief_id)
    if row is None:
        raise HTTPException(status_code=404, detail="brief not found")
    if row.status == "draft" and row.error is None:
        await visit_briefs_repo.mark_sent(db, brief_id)
        await db.commit()
        await db.refresh(row)
    return _visit_brief_to_dto(row)

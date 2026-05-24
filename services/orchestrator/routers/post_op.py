"""Post-op recovery checklist endpoints (task #13).

Extracted from main.py. Create opens a recovery episode + eager-materializes
the checklist and sets the cohort flag; end cancels pending checks and clears
the flag. Post-op day + next check come from the pure post_op math module.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import patients as patients_repo
from app.db.session import get_session

log = logging.getLogger(__name__)

router = APIRouter()


class PostOpCheckDTO(BaseModel):
    key: str
    title: str
    detail: str
    day: int
    target_date: date


class PostOpEpisodeDTO(BaseModel):
    id: int
    patient_id: int
    procedure_name: str
    surgery_date: date
    status: str
    ended_at: datetime | None
    ended_reason: str | None
    post_op_day: int | None
    next_check: PostOpCheckDTO | None


class PostOpCreateRequest(BaseModel):
    procedure_name: str = Field(min_length=1, max_length=255)
    surgery_date: date
    notes: str | None = Field(default=None, max_length=2000)


class PostOpEndRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=255)


def _post_op_to_dto(row: Any, *, on: date | None = None) -> PostOpEpisodeDTO:
    from services.orchestrator import post_op as post_op_math

    on = on or datetime.now(timezone.utc).date()
    day = post_op_math.post_op_day(row.surgery_date, on)
    nc = post_op_math.next_check(row.surgery_date, on=on)
    next_check = None
    if nc is not None:
        check, target = nc
        next_check = PostOpCheckDTO(
            key=check.key,
            title=check.title,
            detail=check.detail,
            day=check.day,
            target_date=target,
        )
    return PostOpEpisodeDTO(
        id=row.id,
        patient_id=row.patient_id,
        procedure_name=row.procedure_name,
        surgery_date=row.surgery_date,
        status=row.status,
        ended_at=row.ended_at,
        ended_reason=row.ended_reason,
        post_op_day=day,
        next_check=next_check,
    )


@router.post("/patients/{patient_id}/post-op", response_model=PostOpEpisodeDTO)
async def create_post_op_episode(
    patient_id: int,
    body: PostOpCreateRequest,
    db: AsyncSession = Depends(get_session),
) -> PostOpEpisodeDTO:
    """Open a post-op recovery episode + eager-materialize the checklist."""
    from app.db.repositories import post_op as post_op_repo
    from services.scheduler import post_op_checklist

    patient = await patients_repo.get(db, patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="patient not found")
    try:
        row = await post_op_repo.create(
            db,
            patient_id=patient_id,
            procedure_name=body.procedure_name,
            surgery_date=body.surgery_date,
            notes=body.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    patient.cohort_post_op = True
    await db.flush()
    if patient.phone:
        try:
            await post_op_checklist.materialize_for_episode(
                db, row, patient_phone=patient.phone
            )
        except Exception:  # noqa: BLE001 — never fail create on this
            log.exception(
                "eager post-op materialize failed for episode %s", row.id
            )
    dto = _post_op_to_dto(row)
    await db.commit()
    return dto


@router.get("/patients/{patient_id}/post-op", response_model=PostOpEpisodeDTO)
async def get_active_post_op(
    patient_id: int,
    db: AsyncSession = Depends(get_session),
) -> PostOpEpisodeDTO:
    from app.db.repositories import post_op as post_op_repo

    row = await post_op_repo.get_active_for_patient(db, patient_id)
    if row is None:
        raise HTTPException(
            status_code=404, detail="no active post-op episode"
        )
    return _post_op_to_dto(row)


@router.post(
    "/patients/{patient_id}/post-op/{episode_id}/end",
    response_model=PostOpEpisodeDTO,
)
async def end_post_op_episode(
    patient_id: int,
    episode_id: int,
    body: PostOpEndRequest,
    db: AsyncSession = Depends(get_session),
) -> PostOpEpisodeDTO:
    from app.db.repositories import post_op as post_op_repo
    from services.scheduler import post_op_checklist

    existing = await post_op_repo.get(db, episode_id)
    if existing is None or existing.patient_id != patient_id:
        raise HTTPException(status_code=404, detail="episode not found")
    row = await post_op_repo.end_episode(db, episode_id, reason=body.reason)
    await post_op_checklist.cancel_for_episode(db, episode_id=episode_id)
    patient = await patients_repo.get(db, patient_id)
    if patient is not None:
        patient.cohort_post_op = False
        await db.flush()
    dto = _post_op_to_dto(row)
    await db.commit()
    return dto

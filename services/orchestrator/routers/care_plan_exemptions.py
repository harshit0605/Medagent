"""Care-plan exemption (patient-level opt-out) endpoints.

Extracted from main.py. An exemption opts a patient out of a specific care
plan's standing order (with a reason + optional expiry), revocable by ops.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import care_plan_exemptions as care_plan_exemptions_repo
from app.db.repositories import care_plans as care_plans_repo
from app.db.repositories import patients as patients_repo
from app.db.session import get_session

router = APIRouter()


class CarePlanExemptionDTO(BaseModel):
    id: int
    patient_id: int
    care_plan_id: int
    care_plan_cohort: str | None = None
    care_plan_test_name: str | None = None
    reason: str
    expires_at: datetime | None
    revoked_at: datetime | None
    created_by: str | None
    revoked_by: str | None
    created_at: datetime
    updated_at: datetime
    is_active: bool


class CarePlanExemptionCreateRequest(BaseModel):
    care_plan_id: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=2000)
    expires_at: datetime | None = None
    created_by: str | None = Field(default=None, max_length=128)


class CarePlanExemptionRevokeRequest(BaseModel):
    revoked_by: str | None = Field(default=None, max_length=128)


def _exemption_to_dto(
    row: Any,
    *,
    plan: Any | None = None,
    now: datetime | None = None,
) -> CarePlanExemptionDTO:
    when = now or datetime.now(timezone.utc)
    expires_at = row.expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    is_active = row.revoked_at is None and (
        expires_at is None or expires_at > when
    )
    return CarePlanExemptionDTO(
        id=row.id,
        patient_id=row.patient_id,
        care_plan_id=row.care_plan_id,
        care_plan_cohort=plan.cohort_attr if plan is not None else None,
        care_plan_test_name=plan.test_name if plan is not None else None,
        reason=row.reason,
        expires_at=expires_at,
        revoked_at=row.revoked_at,
        created_by=row.created_by,
        revoked_by=row.revoked_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
        is_active=is_active,
    )


@router.get(
    "/patients/{patient_id}/care-plan-exemptions",
    response_model=list[CarePlanExemptionDTO],
)
async def list_patient_exemptions(
    patient_id: int,
    db: AsyncSession = Depends(get_session),
    include_inactive: bool = False,
) -> list[CarePlanExemptionDTO]:
    rows = await care_plan_exemptions_repo.list_with_plan_info(
        db, patient_id, include_inactive=include_inactive
    )
    return [_exemption_to_dto(ex, plan=plan) for ex, plan in rows]


@router.post(
    "/patients/{patient_id}/care-plan-exemptions",
    response_model=CarePlanExemptionDTO,
)
async def create_patient_exemption(
    patient_id: int,
    payload: CarePlanExemptionCreateRequest,
    db: AsyncSession = Depends(get_session),
) -> CarePlanExemptionDTO:
    patient = await patients_repo.get(db, patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="patient not found")
    plan = await care_plans_repo.get(db, payload.care_plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="care plan not found")
    if not plan.active:
        raise HTTPException(
            status_code=409,
            detail="cannot exempt a patient from an inactive care plan",
        )

    existing = await care_plan_exemptions_repo.find_active_by_patient_plan(
        db, patient_id=patient_id, care_plan_id=payload.care_plan_id
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "this patient already has an active exemption for this "
                f"plan (id={existing.id}); revoke it before creating a new one"
            ),
        )

    row = await care_plan_exemptions_repo.create(
        db,
        patient_id=patient_id,
        care_plan_id=payload.care_plan_id,
        reason=payload.reason,
        expires_at=payload.expires_at,
        created_by=payload.created_by,
    )
    await db.commit()
    return _exemption_to_dto(row, plan=plan)


@router.post(
    "/care-plan-exemptions/{exemption_id}/revoke",
    response_model=CarePlanExemptionDTO,
)
async def revoke_patient_exemption(
    exemption_id: int,
    payload: CarePlanExemptionRevokeRequest,
    db: AsyncSession = Depends(get_session),
) -> CarePlanExemptionDTO:
    row = await care_plan_exemptions_repo.revoke(
        db, exemption_id, revoked_by=payload.revoked_by
    )
    if row is None:
        raise HTTPException(status_code=404, detail="exemption not found")
    plan = await care_plans_repo.get(db, row.care_plan_id)
    await db.commit()
    return _exemption_to_dto(row, plan=plan)

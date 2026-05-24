"""Multi-patient household endpoints (task #13).

Extracted from main.py. A household groups several patients (members) under one
caregiver contact (1 caregiver → many patients).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import patients as patients_repo
from app.db.session import get_session

router = APIRouter()


class HouseholdMemberDTO(BaseModel):
    id: int
    full_name: str
    phone: str


class HouseholdDTO(BaseModel):
    id: int
    name: str
    primary_caregiver_phone: str | None
    notes: str | None
    created_at: datetime
    members: list[HouseholdMemberDTO]


class HouseholdCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    primary_caregiver_phone: str | None = Field(default=None, max_length=32)
    notes: str | None = Field(default=None, max_length=2000)


class HouseholdMemberAddRequest(BaseModel):
    patient_id: int


def _household_to_dto(row: Any, members: list[Any]) -> HouseholdDTO:
    return HouseholdDTO(
        id=row.id,
        name=row.name,
        primary_caregiver_phone=row.primary_caregiver_phone,
        notes=row.notes,
        created_at=row.created_at,
        members=[
            HouseholdMemberDTO(
                id=m.id, full_name=m.full_name, phone=m.phone
            )
            for m in members
        ],
    )


@router.post("/households", response_model=HouseholdDTO)
async def create_household(
    body: HouseholdCreateRequest,
    db: AsyncSession = Depends(get_session),
) -> HouseholdDTO:
    from app.db.repositories import households as households_repo

    row = await households_repo.create(
        db,
        name=body.name,
        primary_caregiver_phone=body.primary_caregiver_phone,
        notes=body.notes,
    )
    dto = _household_to_dto(row, [])
    await db.commit()
    return dto


@router.get("/households/{household_id}", response_model=HouseholdDTO)
async def get_household(
    household_id: int,
    db: AsyncSession = Depends(get_session),
) -> HouseholdDTO:
    from app.db.repositories import households as households_repo

    row = await households_repo.get(db, household_id)
    if row is None:
        raise HTTPException(status_code=404, detail="household not found")
    members = await households_repo.list_members(db, household_id)
    return _household_to_dto(row, members)


@router.post(
    "/households/{household_id}/members", response_model=HouseholdDTO
)
async def add_household_member(
    household_id: int,
    body: HouseholdMemberAddRequest,
    db: AsyncSession = Depends(get_session),
) -> HouseholdDTO:
    """Link a patient to a household (one caregiver, many patients)."""
    from app.db.repositories import households as households_repo

    updated = await households_repo.add_member(
        db, household_id=household_id, patient_id=body.patient_id
    )
    if updated is None:
        raise HTTPException(
            status_code=404, detail="household or patient not found"
        )
    row = await households_repo.get(db, household_id)
    members = await households_repo.list_members(db, household_id)
    dto = _household_to_dto(row, members)
    await db.commit()
    return dto


@router.get("/patients/{patient_id}/household", response_model=HouseholdDTO)
async def get_patient_household(
    patient_id: int,
    db: AsyncSession = Depends(get_session),
) -> HouseholdDTO:
    from app.db.repositories import households as households_repo

    patient = await patients_repo.get(db, patient_id)
    if patient is None or patient.household_id is None:
        raise HTTPException(
            status_code=404, detail="patient has no household"
        )
    row = await households_repo.get(db, patient.household_id)
    members = await households_repo.list_members(db, patient.household_id)
    return _household_to_dto(row, members)

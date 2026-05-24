"""Cohort-tag (clinician-authored cohort labels) endpoints.

Extracted from main.py. Tags are an alternative to the legacy boolean cohort
columns: clinicians define labels + assign patients, and care plans / broadcasts
can target a tag.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import cohort_tags as cohort_tags_repo
from app.db.repositories import patients as patients_repo
from app.db.session import get_session

router = APIRouter()


class CohortTagDTO(BaseModel):
    id: int
    slug: str
    label: str
    description: str | None
    active: bool
    created_by: str | None
    patient_count: int = 0
    created_at: datetime
    updated_at: datetime


class CohortTagCreateRequest(BaseModel):
    label: str = Field(min_length=1, max_length=128)
    slug: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=2000)
    created_by: str | None = Field(default=None, max_length=128)


class CohortTagUpdateRequest(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    active: bool | None = None


class PatientCohortTagDTO(BaseModel):
    id: int
    patient_id: int
    cohort_tag_id: int
    cohort_tag_slug: str
    cohort_tag_label: str
    assigned_by: str | None
    assigned_at: datetime


class PatientCohortTagAssignRequest(BaseModel):
    cohort_tag_id: int = Field(ge=1)
    assigned_by: str | None = Field(default=None, max_length=128)


def _cohort_tag_to_dto(row: Any, *, patient_count: int = 0) -> CohortTagDTO:
    return CohortTagDTO(
        id=row.id,
        slug=row.slug,
        label=row.label,
        description=row.description,
        active=row.active,
        created_by=row.created_by,
        patient_count=patient_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _assignment_to_dto(
    assignment: Any, tag: Any
) -> PatientCohortTagDTO:
    return PatientCohortTagDTO(
        id=assignment.id,
        patient_id=assignment.patient_id,
        cohort_tag_id=assignment.cohort_tag_id,
        cohort_tag_slug=tag.slug,
        cohort_tag_label=tag.label,
        assigned_by=assignment.assigned_by,
        assigned_at=assignment.assigned_at,
    )


@router.get("/cohort-tags", response_model=list[CohortTagDTO])
async def list_cohort_tags(
    db: AsyncSession = Depends(get_session),
    include_inactive: bool = False,
) -> list[CohortTagDTO]:
    rows = (
        await cohort_tags_repo.list_all(db)
        if include_inactive
        else await cohort_tags_repo.list_active(db)
    )
    out: list[CohortTagDTO] = []
    for row in rows:
        count = await cohort_tags_repo.patient_count(db, row.id)
        out.append(_cohort_tag_to_dto(row, patient_count=count))
    return out


@router.post("/cohort-tags", response_model=CohortTagDTO)
async def create_cohort_tag(
    payload: CohortTagCreateRequest,
    db: AsyncSession = Depends(get_session),
) -> CohortTagDTO:
    slug = payload.slug or cohort_tags_repo.slugify(payload.label)
    existing = await cohort_tags_repo.find_by_slug(db, slug)
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"cohort tag with slug {slug!r} already exists "
                f"(id={existing.id}); rename or reactivate it instead"
            ),
        )
    row = await cohort_tags_repo.create(
        db,
        label=payload.label,
        slug=slug,
        description=payload.description,
        created_by=payload.created_by,
    )
    await db.commit()
    return _cohort_tag_to_dto(row, patient_count=0)


@router.put("/cohort-tags/{tag_id}", response_model=CohortTagDTO)
async def update_cohort_tag(
    tag_id: int,
    payload: CohortTagUpdateRequest,
    db: AsyncSession = Depends(get_session),
) -> CohortTagDTO:
    row = await cohort_tags_repo.update(
        db,
        tag_id,
        label=payload.label,
        description=payload.description,
        active=payload.active,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="cohort tag not found")
    await db.commit()
    count = await cohort_tags_repo.patient_count(db, row.id)
    return _cohort_tag_to_dto(row, patient_count=count)


@router.get(
    "/patients/{patient_id}/cohort-tags",
    response_model=list[PatientCohortTagDTO],
)
async def list_patient_cohort_tags(
    patient_id: int, db: AsyncSession = Depends(get_session)
) -> list[PatientCohortTagDTO]:
    rows = await cohort_tags_repo.list_for_patient(db, patient_id)
    return [_assignment_to_dto(a, t) for a, t in rows]


@router.post(
    "/patients/{patient_id}/cohort-tags",
    response_model=PatientCohortTagDTO,
)
async def assign_patient_cohort_tag(
    patient_id: int,
    payload: PatientCohortTagAssignRequest,
    db: AsyncSession = Depends(get_session),
) -> PatientCohortTagDTO:
    patient = await patients_repo.get(db, patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="patient not found")
    tag = await cohort_tags_repo.get(db, payload.cohort_tag_id)
    if tag is None:
        raise HTTPException(status_code=404, detail="cohort tag not found")
    if not tag.active:
        raise HTTPException(
            status_code=409,
            detail="cannot assign an inactive cohort tag to a patient",
        )
    assignment = await cohort_tags_repo.assign(
        db,
        patient_id=patient_id,
        cohort_tag_id=payload.cohort_tag_id,
        assigned_by=payload.assigned_by,
    )
    await db.commit()
    return _assignment_to_dto(assignment, tag)


@router.delete(
    "/patients/{patient_id}/cohort-tags/{tag_id}", status_code=204
)
async def remove_patient_cohort_tag(
    patient_id: int,
    tag_id: int,
    db: AsyncSession = Depends(get_session),
) -> None:
    removed = await cohort_tags_repo.remove(
        db, patient_id=patient_id, cohort_tag_id=tag_id
    )
    if not removed:
        raise HTTPException(
            status_code=404,
            detail="patient is not currently assigned to that cohort tag",
        )
    await db.commit()

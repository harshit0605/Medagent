"""Care-plan (HEDIS-style cohort standing orders) endpoints.

Extracted from main.py. A plan targets exactly one cohort dimension — a legacy
boolean ``cohort_attr`` or a clinician-authored ``cohort_tag_id``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import care_plans as care_plans_repo
from app.db.repositories import cohort_tags as cohort_tags_repo
from app.db.session import get_session

router = APIRouter()


class CarePlanDTO(BaseModel):
    id: int
    cohort_attr: str | None = None
    cohort_tag_id: int | None = None
    cohort_tag_label: str | None = None
    cohort_tag_slug: str | None = None
    test_name: str
    cadence_days: int
    due_in_days: int
    active: bool
    notes: str | None
    created_by: str | None
    created_at: datetime
    updated_at: datetime


class CarePlanCreateRequest(BaseModel):
    """Plan must reference EXACTLY ONE cohort dimension. Pass either
    ``cohort_attr`` (legacy boolean column on patients) or
    ``cohort_tag_id`` (clinician-authored tag) — but not both."""

    cohort_attr: str | None = Field(default=None, max_length=64)
    cohort_tag_id: int | None = Field(default=None, ge=1)
    test_name: str = Field(min_length=1, max_length=255)
    cadence_days: int = Field(ge=1, le=3650)
    due_in_days: int = Field(default=14, ge=0, le=365)
    notes: str | None = Field(default=None, max_length=2000)
    created_by: str | None = Field(default=None, max_length=128)


class CarePlanUpdateRequest(BaseModel):
    cadence_days: int | None = Field(default=None, ge=1, le=3650)
    due_in_days: int | None = Field(default=None, ge=0, le=365)
    active: bool | None = None
    notes: str | None = Field(default=None, max_length=2000)


class CarePlanCohortOptionDTO(BaseModel):
    """One option for the ops-console cohort picker. ``kind`` is either
    ``boolean`` (legacy column) or ``tag`` (clinician-authored). The
    UI passes the right field on create — cohort_attr for boolean,
    cohort_tag_id for tag."""

    kind: Literal["boolean", "tag"]
    cohort_attr: str | None = None
    cohort_tag_id: int | None = None
    label: str
    description: str | None = None


def _care_plan_to_dto(
    row: Any, *, tag: Any | None = None
) -> CarePlanDTO:
    return CarePlanDTO(
        id=row.id,
        cohort_attr=row.cohort_attr,
        cohort_tag_id=row.cohort_tag_id,
        cohort_tag_label=tag.label if tag is not None else None,
        cohort_tag_slug=tag.slug if tag is not None else None,
        test_name=row.test_name,
        cadence_days=row.cadence_days,
        due_in_days=row.due_in_days,
        active=row.active,
        notes=row.notes,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _denormalize_plan(
    db: AsyncSession, row: Any
) -> CarePlanDTO:
    """Resolve the tag row for a plan if it has one, so the DTO carries
    label + slug. Cheap — one extra get() per plan, only when tag-based."""
    tag = None
    if row.cohort_tag_id is not None:
        tag = await cohort_tags_repo.get(db, row.cohort_tag_id)
    return _care_plan_to_dto(row, tag=tag)


def _validate_cohort_attr(cohort_attr: str) -> None:
    if cohort_attr not in care_plans_repo.KNOWN_COHORT_ATTRS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unknown cohort_attr {cohort_attr!r}; allowed: "
                f"{', '.join(care_plans_repo.KNOWN_COHORT_ATTRS)}"
            ),
        )


@router.get("/care-plans", response_model=list[CarePlanDTO])
async def list_care_plans(
    db: AsyncSession = Depends(get_session),
    include_inactive: bool = False,
) -> list[CarePlanDTO]:
    rows = (
        await care_plans_repo.list_all(db)
        if include_inactive
        else await care_plans_repo.list_active(db)
    )
    # Resolve tags in one batch so we don't fan out N+1 queries.
    tag_ids = {r.cohort_tag_id for r in rows if r.cohort_tag_id is not None}
    tags_by_id: dict[int, Any] = {}
    for tid in tag_ids:
        t = await cohort_tags_repo.get(db, tid)
        if t is not None:
            tags_by_id[tid] = t
    return [
        _care_plan_to_dto(
            r, tag=tags_by_id.get(r.cohort_tag_id) if r.cohort_tag_id else None
        )
        for r in rows
    ]


@router.get(
    "/care-plans/cohorts", response_model=list[CarePlanCohortOptionDTO]
)
async def list_care_plan_cohorts(
    db: AsyncSession = Depends(get_session),
) -> list[CarePlanCohortOptionDTO]:
    """Cohort picker options: legacy boolean cohorts + active
    clinician-authored tags. The ops console shows them in one
    dropdown; the create endpoint expects ``cohort_attr`` for boolean
    or ``cohort_tag_id`` for tag-based."""
    options: list[CarePlanCohortOptionDTO] = []
    for attr in care_plans_repo.KNOWN_COHORT_ATTRS:
        # Friendly label = strip the cohort_ prefix and title-case the rest
        label = attr.replace("cohort_", "").replace("_", " ").title()
        options.append(
            CarePlanCohortOptionDTO(
                kind="boolean",
                cohort_attr=attr,
                label=label,
            )
        )
    tags = await cohort_tags_repo.list_active(db)
    for tag in tags:
        options.append(
            CarePlanCohortOptionDTO(
                kind="tag",
                cohort_tag_id=tag.id,
                label=tag.label,
                description=tag.description,
            )
        )
    return options


def _validate_create_cohort_choice(
    payload: CarePlanCreateRequest,
) -> None:
    """Enforce exactly one of (cohort_attr, cohort_tag_id) is set."""
    has_attr = payload.cohort_attr is not None
    has_tag = payload.cohort_tag_id is not None
    if has_attr == has_tag:
        raise HTTPException(
            status_code=400,
            detail=(
                "exactly one of cohort_attr or cohort_tag_id is required"
            ),
        )


@router.post("/care-plans", response_model=CarePlanDTO)
async def create_care_plan(
    payload: CarePlanCreateRequest,
    db: AsyncSession = Depends(get_session),
) -> CarePlanDTO:
    _validate_create_cohort_choice(payload)

    if payload.cohort_attr is not None:
        _validate_cohort_attr(payload.cohort_attr)
        existing = await care_plans_repo.find_by_cohort_test(
            db,
            cohort_attr=payload.cohort_attr,
            test_name=payload.test_name,
        )
    else:
        # Tag must exist + be active.
        tag = await cohort_tags_repo.get(db, payload.cohort_tag_id or 0)
        if tag is None:
            raise HTTPException(status_code=404, detail="cohort tag not found")
        if not tag.active:
            raise HTTPException(
                status_code=409,
                detail="cannot create a care plan against an inactive cohort tag",
            )
        existing = await care_plans_repo.find_by_cohort_test(
            db,
            cohort_tag_id=payload.cohort_tag_id,
            test_name=payload.test_name,
        )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "a care plan for this cohort + test already exists "
                f"(id={existing.id}); edit or deactivate it instead"
            ),
        )
    row = await care_plans_repo.create(
        db,
        cohort_attr=payload.cohort_attr,
        cohort_tag_id=payload.cohort_tag_id,
        test_name=payload.test_name.strip(),
        cadence_days=payload.cadence_days,
        due_in_days=payload.due_in_days,
        notes=payload.notes,
        created_by=payload.created_by,
    )
    await db.commit()
    return await _denormalize_plan(db, row)


@router.put("/care-plans/{plan_id}", response_model=CarePlanDTO)
async def update_care_plan(
    plan_id: int,
    payload: CarePlanUpdateRequest,
    db: AsyncSession = Depends(get_session),
) -> CarePlanDTO:
    row = await care_plans_repo.update(
        db,
        plan_id,
        cadence_days=payload.cadence_days,
        due_in_days=payload.due_in_days,
        active=payload.active,
        notes=payload.notes,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="care plan not found")
    await db.commit()
    return await _denormalize_plan(db, row)


@router.post("/care-plans/{plan_id}/deactivate", response_model=CarePlanDTO)
async def deactivate_care_plan(
    plan_id: int, db: AsyncSession = Depends(get_session)
) -> CarePlanDTO:
    row = await care_plans_repo.deactivate(db, plan_id)
    if row is None:
        raise HTTPException(status_code=404, detail="care plan not found")
    await db.commit()
    return await _denormalize_plan(db, row)

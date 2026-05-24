"""Care-plan goal + observation endpoints (slice 14).

Extracted from main.py. Per-patient quantitative goals (e.g. HbA1c < 7.0) plus
the observations recorded against them, with inline latest-value / on-target /
drift computation for the ops console.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import patients as patients_repo
from app.db.session import get_session

router = APIRouter()


class CarePlanGoalDTO(BaseModel):
    """Per-patient quantitative goal. Distinct from
    ``CarePlanDTO`` (cohort-level template)."""

    id: int
    patient_id: int
    metric_key: str
    metric_label: str
    target_value: float
    comparator: str
    target_unit: str
    status: str
    ends_on: date | None
    created_by: str | None
    notes: str | None
    created_at: datetime
    updated_at: datetime
    # Convenience: the most recent observation, when any.
    # Pre-fetched server-side so the patient detail page
    # doesn't N+1 to fill the "current value" column.
    latest_value: float | None = None
    latest_observed_at: datetime | None = None
    # Computed against the latest_value: ``True`` when the
    # patient is meeting the target, ``None`` when no
    # observations exist yet.
    on_target: bool | None = None
    # Trend classification (slice 17): ``on_target`` /
    # ``slipping`` / ``persistent_off`` / ``stale`` /
    # ``no_data``. Surfaced inline so the UI can render a
    # drift badge without joining ops_tickets.
    drift_status: str | None = None


class MetricObservationDTO(BaseModel):
    id: int
    patient_id: int
    goal_id: int | None
    metric_key: str
    value: float
    unit: str
    observed_at: datetime
    source: str
    recorded_by: str | None
    notes: str | None
    created_at: datetime


def _eval_on_target(
    *, value: float, comparator: str, target: float
) -> bool:
    """Pure helper. Lives next to the DTO conversion so the
    same logic is used by every read path."""
    if comparator == "less_than":
        return value < target
    if comparator == "greater_than":
        return value > target
    # Future: ``between`` would carry a (low, high) tuple
    # in target_value (or a separate column). For v1 only
    # less/greater are allowed at write time.
    return False


def _goal_to_dto(
    row: Any,
    *,
    latest_value: float | None = None,
    latest_observed_at: datetime | None = None,
    drift_status: str | None = None,
) -> CarePlanGoalDTO:
    on_target: bool | None
    if latest_value is None:
        on_target = None
    else:
        on_target = _eval_on_target(
            value=latest_value,
            comparator=row.comparator,
            target=float(row.target_value),
        )
    return CarePlanGoalDTO(
        id=row.id,
        patient_id=row.patient_id,
        metric_key=row.metric_key,
        metric_label=row.metric_label,
        target_value=float(row.target_value),
        comparator=row.comparator,
        target_unit=row.target_unit,
        status=row.status,
        ends_on=row.ends_on,
        created_by=row.created_by,
        notes=row.notes,
        created_at=row.created_at,
        updated_at=row.updated_at,
        latest_value=latest_value,
        latest_observed_at=latest_observed_at,
        on_target=on_target,
        drift_status=drift_status,
    )


def _observation_to_dto(row: Any) -> MetricObservationDTO:
    return MetricObservationDTO(
        id=row.id,
        patient_id=row.patient_id,
        goal_id=row.goal_id,
        metric_key=row.metric_key,
        value=float(row.value),
        unit=row.unit,
        observed_at=row.observed_at,
        source=row.source,
        recorded_by=row.recorded_by,
        notes=row.notes,
        created_at=row.created_at,
    )


class CarePlanGoalCreateRequest(BaseModel):
    metric_key: str = Field(min_length=1, max_length=64)
    metric_label: str = Field(min_length=1, max_length=128)
    target_value: float
    comparator: Literal["less_than", "greater_than"] = "less_than"
    target_unit: str = Field(min_length=1, max_length=32)
    ends_on: date | None = None
    created_by: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=2000)


class CarePlanGoalStatusRequest(BaseModel):
    status: Literal["active", "achieved", "inactive"]


class MetricObservationCreateRequest(BaseModel):
    value: float
    unit: str = Field(min_length=1, max_length=32)
    observed_at: datetime | None = None
    source: Literal[
        "manual", "patient_self_report", "lab", "device"
    ] = "manual"
    recorded_by: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=2000)


@router.post(
    "/patients/{patient_id}/goals",
    response_model=CarePlanGoalDTO,
)
async def create_patient_goal(
    patient_id: int,
    body: CarePlanGoalCreateRequest,
    db: AsyncSession = Depends(get_session),
) -> CarePlanGoalDTO:
    from app.db.repositories import (
        care_plan_goals as goals_repo,
    )

    patient = await patients_repo.get(db, patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="patient not found")

    try:
        row = await goals_repo.create_goal(
            db,
            patient_id=patient_id,
            metric_key=body.metric_key,
            metric_label=body.metric_label,
            target_value=Decimal(str(body.target_value)),
            comparator=body.comparator,
            target_unit=body.target_unit,
            created_by=body.created_by,
            ends_on=body.ends_on,
            notes=body.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    dto = _goal_to_dto(row)
    await db.commit()
    return dto


@router.get(
    "/patients/{patient_id}/goals",
    response_model=list[CarePlanGoalDTO],
)
async def list_patient_goals(
    patient_id: int,
    status: str | None = Query(
        default=None,
        description=(
            "Filter by status (active / achieved / inactive). "
            "Omit to get all (newest first)."
        ),
    ),
    db: AsyncSession = Depends(get_session),
) -> list[CarePlanGoalDTO]:
    """List goals + the most recent observation per goal so
    the UI can render the current value + on-target badge
    without an N+1 round trip."""
    from app.db.repositories import (
        care_plan_goals as goals_repo,
    )

    try:
        goals = await goals_repo.list_goals_for_patient(
            db, patient_id, status=status
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    out: list[CarePlanGoalDTO] = []
    for g in goals:
        # Pull the last 3 observations — enough to evaluate
        # drift (slipping needs to look back 3 values) AND
        # cover the "latest value" column the UI shows.
        obs = await goals_repo.list_observations_for_goal(
            db, g.id, limit=3
        )
        latest_value = float(obs[0].value) if obs else None
        latest_observed_at = obs[0].observed_at if obs else None
        # Drift only meaningful for active goals. Achieved /
        # inactive goals don't need a drift label — they're
        # archived workflows.
        drift = None
        if g.status == "active":
            drift = goals_repo.evaluate_drift_status(g, obs)
        out.append(
            _goal_to_dto(
                g,
                latest_value=latest_value,
                latest_observed_at=latest_observed_at,
                drift_status=drift,
            )
        )
    return out


@router.patch(
    "/patients/{patient_id}/goals/{goal_id}/status",
    response_model=CarePlanGoalDTO,
)
async def update_patient_goal_status(
    patient_id: int,
    goal_id: int,
    body: CarePlanGoalStatusRequest,
    db: AsyncSession = Depends(get_session),
) -> CarePlanGoalDTO:
    from app.db.repositories import (
        care_plan_goals as goals_repo,
    )

    existing = await goals_repo.get_goal(db, goal_id)
    if existing is None or existing.patient_id != patient_id:
        raise HTTPException(status_code=404, detail="goal not found")
    try:
        row = await goals_repo.update_status(
            db, goal_id, status=body.status
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if row is None:
        raise HTTPException(status_code=404, detail="goal not found")

    obs = await goals_repo.list_observations_for_goal(
        db, goal_id, limit=3
    )
    latest_value = float(obs[0].value) if obs else None
    latest_observed_at = obs[0].observed_at if obs else None
    drift = None
    if row.status == "active":
        drift = goals_repo.evaluate_drift_status(row, obs)
    dto = _goal_to_dto(
        row,
        latest_value=latest_value,
        latest_observed_at=latest_observed_at,
        drift_status=drift,
    )
    await db.commit()
    return dto


@router.post(
    "/patients/{patient_id}/goals/{goal_id}/observations",
    response_model=MetricObservationDTO,
)
async def record_goal_observation(
    patient_id: int,
    goal_id: int,
    body: MetricObservationCreateRequest,
    db: AsyncSession = Depends(get_session),
) -> MetricObservationDTO:
    """Record a measurement against a specific goal. The
    goal's ``metric_key`` + ``unit`` are the source of truth —
    the observation inherits them so the operator can't
    accidentally log mmHg against an HbA1c goal."""
    from app.db.repositories import (
        care_plan_goals as goals_repo,
    )

    goal = await goals_repo.get_goal(db, goal_id)
    if goal is None or goal.patient_id != patient_id:
        raise HTTPException(status_code=404, detail="goal not found")

    try:
        row = await goals_repo.record_observation(
            db,
            patient_id=patient_id,
            goal_id=goal_id,
            metric_key=goal.metric_key,
            value=Decimal(str(body.value)),
            unit=body.unit or goal.target_unit,
            observed_at=body.observed_at,
            source=body.source,
            recorded_by=body.recorded_by,
            notes=body.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    dto = _observation_to_dto(row)
    await db.commit()
    return dto


@router.get(
    "/patients/{patient_id}/goals/{goal_id}/observations",
    response_model=list[MetricObservationDTO],
)
async def list_goal_observations(
    patient_id: int,
    goal_id: int,
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_session),
) -> list[MetricObservationDTO]:
    from app.db.repositories import (
        care_plan_goals as goals_repo,
    )

    goal = await goals_repo.get_goal(db, goal_id)
    if goal is None or goal.patient_id != patient_id:
        raise HTTPException(status_code=404, detail="goal not found")
    rows = await goals_repo.list_observations_for_goal(
        db, goal_id, limit=limit
    )
    return [_observation_to_dto(r) for r in rows]

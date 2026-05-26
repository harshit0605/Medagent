"""Pregnancy timeline endpoints (task #10).

Extracted from main.py. Create opens a pregnancy + eagerly materializes the
first reminders and sets the cohort flag; end cancels pending reminders and
clears the flag. Gestational age + next milestone are computed via the pure
pregnancy math module.
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


class PregnancyMilestoneDTO(BaseModel):
    key: str
    kind: str
    title: str
    detail: str
    week: int
    target_date: date


class PregnancyDTO(BaseModel):
    """Active (or ended) pregnancy with computed gestational age + the next
    upcoming milestone. Gestational fields are computed as of *today*."""

    id: int
    patient_id: int
    lmp_date: date | None
    edd: date | None
    status: str
    ended_at: datetime | None
    ended_reason: str | None
    gestational_week: int | None
    gestational_days: int | None
    trimester: int | None
    next_milestone: PregnancyMilestoneDTO | None


class PregnancyCreateRequest(BaseModel):
    """Open a pregnancy timeline. At least one of ``lmp_date`` / ``edd`` is
    required; the engine derives the other (Naegele's rule)."""

    lmp_date: date | None = None
    edd: date | None = None
    notes: str | None = Field(default=None, max_length=2000)


class PregnancyEndRequest(BaseModel):
    """End-of-pregnancy intake. When ``birth_outcome == 'delivered'`` AND
    ``delivery_date`` is provided, the endpoint also flips the row into its
    postpartum phase and eagerly materializes the first PP reminders. Any
    other ``birth_outcome`` (miscarriage / stillbirth / termination /
    unknown) just closes the episode — no PP cadence."""

    reason: str | None = Field(default=None, max_length=255)
    birth_outcome: str | None = Field(default=None, max_length=32)
    delivery_date: date | None = None


class PostpartumMilestoneDTO(BaseModel):
    key: str
    kind: str
    title: str
    detail: str
    day: int
    target_date: date


class PostpartumDTO(BaseModel):
    """Active (or recently-closed) postpartum episode with computed PP age
    and the next upcoming milestone. PP fields are computed as of *today*."""

    pregnancy_id: int
    patient_id: int
    delivery_date: date | None
    birth_outcome: str | None
    postpartum_active: bool
    postpartum_ended_at: datetime | None
    postpartum_ended_reason: str | None
    pp_week: int | None
    pp_day: int | None
    next_milestone: PostpartumMilestoneDTO | None


class PostpartumEndRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=255)


def _postpartum_to_dto(row: Any, *, on: date | None = None) -> PostpartumDTO:
    from services.orchestrator import postpartum as pp_math

    on = on or datetime.now(timezone.utc).date()
    pp_week = pp_day = None
    next_ms: PostpartumMilestoneDTO | None = None
    if row.delivery_date is not None:
        pp_day = pp_math.postpartum_days(row.delivery_date, on)
        pp_week = pp_math.postpartum_week(row.delivery_date, on)
        nm = pp_math.next_milestone(row.delivery_date, on=on)
        if nm is not None:
            milestone, target = nm
            next_ms = PostpartumMilestoneDTO(
                key=milestone.key,
                kind=milestone.kind,
                title=milestone.title,
                detail=milestone.detail,
                day=milestone.day,
                target_date=target,
            )
    return PostpartumDTO(
        pregnancy_id=row.id,
        patient_id=row.patient_id,
        delivery_date=row.delivery_date,
        birth_outcome=row.birth_outcome,
        postpartum_active=row.postpartum_active,
        postpartum_ended_at=row.postpartum_ended_at,
        postpartum_ended_reason=row.postpartum_ended_reason,
        pp_week=pp_week,
        pp_day=pp_day,
        next_milestone=next_ms,
    )


def _pregnancy_to_dto(row: Any, *, on: date | None = None) -> PregnancyDTO:
    from services.orchestrator import pregnancy as preg_math

    on = on or datetime.now(timezone.utc).date()
    week = days = tri = None
    next_ms: PregnancyMilestoneDTO | None = None
    try:
        lmp, _edd = preg_math.resolve_lmp_edd(row.lmp_date, row.edd)
    except ValueError:
        lmp = None
    if lmp is not None:
        week, days = preg_math.gestational_age(lmp, on)
        tri = preg_math.trimester(week)
        nm = preg_math.next_milestone(lmp, on=on)
        if nm is not None:
            milestone, target = nm
            next_ms = PregnancyMilestoneDTO(
                key=milestone.key,
                kind=milestone.kind,
                title=milestone.title,
                detail=milestone.detail,
                week=milestone.week,
                target_date=target,
            )
    return PregnancyDTO(
        id=row.id,
        patient_id=row.patient_id,
        lmp_date=row.lmp_date,
        edd=row.edd,
        status=row.status,
        ended_at=row.ended_at,
        ended_reason=row.ended_reason,
        gestational_week=week,
        gestational_days=days,
        trimester=tri,
        next_milestone=next_ms,
    )


@router.post(
    "/patients/{patient_id}/pregnancy",
    response_model=PregnancyDTO,
)
async def create_pregnancy(
    patient_id: int,
    body: PregnancyCreateRequest,
    db: AsyncSession = Depends(get_session),
) -> PregnancyDTO:
    """Open an active pregnancy timeline for a patient and immediately
    materialize the upcoming milestone + weekly-check-in reminders (so they're
    queued without waiting for the next scheduler sweep)."""
    from app.db.repositories import pregnancies as pregnancies_repo
    from services.orchestrator import pregnancy as preg_math
    from services.scheduler import pregnancy_milestones

    patient = await patients_repo.get(db, patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="patient not found")

    # Resolve so both LMP + EDD are stored (the engine + UI both benefit).
    try:
        lmp, edd = preg_math.resolve_lmp_edd(body.lmp_date, body.edd)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        row = await pregnancies_repo.create(
            db,
            patient_id=patient_id,
            lmp_date=lmp,
            edd=edd,
            notes=body.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    # Keep the first-class cohort flag in sync so broadcasts / care-plan
    # targeting / dashboards see this patient as pregnant.
    patient.cohort_pregnancy = True
    await db.flush()

    # Materialize the first batch of reminders eagerly.
    if patient.phone:
        try:
            await pregnancy_milestones.materialize_for_pregnancy(
                db, row, patient_phone=patient.phone
            )
        except Exception:  # noqa: BLE001 — never fail the create on this
            log.exception(
                "eager pregnancy materialize failed for pregnancy %s", row.id
            )

    dto = _pregnancy_to_dto(row)
    await db.commit()
    return dto


@router.get(
    "/patients/{patient_id}/pregnancy",
    response_model=PregnancyDTO,
)
async def get_active_pregnancy(
    patient_id: int,
    db: AsyncSession = Depends(get_session),
) -> PregnancyDTO:
    """The patient's current active pregnancy with computed gestational age
    and next milestone. 404 if there's no active pregnancy."""
    from app.db.repositories import pregnancies as pregnancies_repo

    row = await pregnancies_repo.get_active_for_patient(db, patient_id)
    if row is None:
        raise HTTPException(
            status_code=404, detail="no active pregnancy for patient"
        )
    return _pregnancy_to_dto(row)


@router.post(
    "/patients/{patient_id}/pregnancy/{pregnancy_id}/end",
    response_model=PregnancyDTO,
)
async def end_pregnancy(
    patient_id: int,
    pregnancy_id: int,
    body: PregnancyEndRequest,
    db: AsyncSession = Depends(get_session),
) -> PregnancyDTO:
    """End a pregnancy episode (delivered / miscarried / corrected) and cancel
    every pending milestone + weekly reminder for it.

    Birth-outcome routing:
      * ``delivered`` + ``delivery_date`` set → auto-flip to postpartum phase
        and eagerly materialize PP reminders (early visit, EPDS screens,
        6-week visit + contraception, baby vaccines, final close).
      * Anything else (miscarriage / stillbirth / termination / unknown /
        omitted) → just close, no PP cadence. The clinical team can open a
        bereavement-care ticket separately.
    """
    from app.db.repositories import pregnancies as pregnancies_repo
    from services.scheduler import postpartum_milestones, pregnancy_milestones

    existing = await pregnancies_repo.get(db, pregnancy_id)
    if existing is None or existing.patient_id != patient_id:
        raise HTTPException(status_code=404, detail="pregnancy not found")

    row = await pregnancies_repo.end_pregnancy(
        db,
        pregnancy_id,
        reason=body.reason,
        birth_outcome=body.birth_outcome,
        delivery_date=body.delivery_date,
    )
    await pregnancy_milestones.cancel_for_pregnancy(
        db, pregnancy_id=pregnancy_id
    )
    # Clear the pregnancy cohort flag — the pre-delivery episode is over.
    patient = await patients_repo.get(db, patient_id)
    if patient is not None:
        patient.cohort_pregnancy = False
        await db.flush()

    # Transition into postpartum if the outcome is delivered and we have
    # a delivery date to anchor the schedule. Materialize eagerly so the
    # operator sees the first nudges queued without waiting for the sweep.
    if (
        row is not None
        and body.birth_outcome == "delivered"
        and body.delivery_date is not None
    ):
        try:
            await pregnancies_repo.start_postpartum(
                db, pregnancy_id, delivery_date=body.delivery_date
            )
            if patient is not None and patient.phone:
                await postpartum_milestones.materialize_for_pregnancy(
                    db, row, patient_phone=patient.phone
                )
        except ValueError as exc:
            # Validation failure (e.g. patient already has a different PP
            # row open) shouldn't block the end-pregnancy commit — log and
            # let the operator triage manually.
            log.warning(
                "skipped postpartum transition for pregnancy %s: %s",
                pregnancy_id,
                exc,
            )
        except Exception:  # noqa: BLE001 — never fail end on PP setup
            log.exception(
                "eager postpartum materialize failed for pregnancy %s",
                pregnancy_id,
            )

    dto = _pregnancy_to_dto(row)
    await db.commit()
    return dto


# ---- postpartum endpoints --------------------------------------------------


@router.get(
    "/patients/{patient_id}/postpartum",
    response_model=PostpartumDTO,
)
async def get_active_postpartum(
    patient_id: int,
    db: AsyncSession = Depends(get_session),
) -> PostpartumDTO:
    """The patient's current postpartum episode (the previously-delivered
    pregnancy now in its PP phase). 404 if no active PP."""
    from app.db.repositories import pregnancies as pregnancies_repo

    row = await pregnancies_repo.get_postpartum_active_for_patient(
        db, patient_id
    )
    if row is None:
        raise HTTPException(
            status_code=404, detail="no active postpartum for patient"
        )
    return _postpartum_to_dto(row)


@router.post(
    "/patients/{patient_id}/postpartum/{pregnancy_id}/end",
    response_model=PostpartumDTO,
)
async def end_postpartum(
    patient_id: int,
    pregnancy_id: int,
    body: PostpartumEndRequest,
    db: AsyncSession = Depends(get_session),
) -> PostpartumDTO:
    """Close a postpartum episode early (e.g. patient transferred care, or
    PP review wrapped up before the 12-week boundary) and cancel every
    pending PP reminder for it. Idempotent — re-closing leaves the original
    ``postpartum_ended_at`` timestamp intact."""
    from app.db.repositories import pregnancies as pregnancies_repo
    from services.scheduler import postpartum_milestones

    existing = await pregnancies_repo.get(db, pregnancy_id)
    if existing is None or existing.patient_id != patient_id:
        raise HTTPException(status_code=404, detail="pregnancy not found")

    row = await pregnancies_repo.end_postpartum(
        db, pregnancy_id, reason=body.reason
    )
    await postpartum_milestones.cancel_for_pregnancy(
        db, pregnancy_id=pregnancy_id
    )
    dto = _postpartum_to_dto(row)
    await db.commit()
    return dto

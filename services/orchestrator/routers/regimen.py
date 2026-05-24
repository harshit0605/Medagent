"""Regimen + dose/refill test-trigger endpoints.

Extracted from main.py. Regimen CRUD (create eager-materializes the next dose
reminders; deactivate cancels them) plus demo "fire a dose/refill reminder NOW"
triggers. ``RegimenDTO`` / ``_regimen_to_dto`` come from the shared
routers._dtos module.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import adherence_events as adherence_events_repo
from app.db.repositories import patients as patients_repo
from app.db.repositories import regimens as regimens_repo
from app.db.repositories import scheduled_events as scheduled_events_repo
from app.db.session import get_session
from services.orchestrator.routers._dtos import RegimenDTO, _regimen_to_dto
from services.scheduler import dose_reminders

log = logging.getLogger(__name__)

router = APIRouter()


class RegimenScheduleDTO(BaseModel):
    type: Literal["times_of_day"] = "times_of_day"
    times: list[str] = Field(default_factory=list)
    timezone: str = "Asia/Kolkata"
    frequency: Literal["daily"] = "daily"


class RegimenCreateRequest(BaseModel):
    medication_name: str = Field(min_length=1, max_length=255)
    dose: str = Field(min_length=1, max_length=128)
    schedule: RegimenScheduleDTO
    starts_on: date | None = None
    ends_on: date | None = None
    strict_timing: bool = False
    supply_days_initial: int | None = Field(default=None, ge=1, le=365)
    supply_started_on: date | None = None


@router.post("/patients/{patient_id}/regimens", response_model=RegimenDTO)
async def create_regimen(
    patient_id: int,
    payload: RegimenCreateRequest,
    db: AsyncSession = Depends(get_session),
) -> RegimenDTO:
    """Create a regimen for a patient and immediately materialize the next
    48h of dose reminders so the patient gets their next prompt without
    waiting for the periodic loop."""
    patient = await patients_repo.get(db, patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="patient not found")
    row = await regimens_repo.create(
        db,
        patient_id=patient_id,
        medication_name=payload.medication_name,
        dose=payload.dose,
        schedule=payload.schedule.model_dump(),
        starts_on=payload.starts_on,
        ends_on=payload.ends_on,
        strict_timing=payload.strict_timing,
        supply_days_initial=payload.supply_days_initial,
        # Default supply_started_on to today when supply tracking is enabled
        # but the caller didn't specify a start date — most common case.
        supply_started_on=(
            payload.supply_started_on
            or (
                datetime.now(timezone.utc).date()
                if payload.supply_days_initial is not None
                else None
            )
        ),
    )
    try:
        await dose_reminders.materialize_for_regimen(
            db, row, patient_phone=patient.phone
        )
    except Exception:  # noqa: BLE001
        log.exception("immediate materialize failed for regimen=%s", row.id)
    await db.commit()
    return _regimen_to_dto(row)


@router.get("/patients/{patient_id}/regimens", response_model=list[RegimenDTO])
async def list_patient_regimens(
    patient_id: int,
    db: AsyncSession = Depends(get_session),
) -> list[RegimenDTO]:
    rows = await regimens_repo.list_for_patient(db, patient_id)
    return [_regimen_to_dto(r) for r in rows]


@router.post("/regimens/{regimen_id}/deactivate", response_model=RegimenDTO)
async def deactivate_regimen(
    regimen_id: int,
    db: AsyncSession = Depends(get_session),
) -> RegimenDTO:
    row = await regimens_repo.deactivate(
        db, regimen_id, on=datetime.now(timezone.utc).date()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="regimen not found")
    await dose_reminders.cancel_for_regimen(db, regimen_id=regimen_id)
    await db.commit()
    return _regimen_to_dto(row)


class TestDoseResponse(BaseModel):
    scheduled_event_id: int
    adherence_event_id: int
    scheduled_for: datetime
    note: str


@router.post(
    "/regimens/{regimen_id}/test-dose",
    response_model=TestDoseResponse,
)
async def fire_test_dose(
    regimen_id: int,
    db: AsyncSession = Depends(get_session),
) -> TestDoseResponse:
    """Enqueue a single dose reminder due NOW for this regimen — useful for
    demos where you don't want to wait for a real scheduled time."""
    regimen = await regimens_repo.get(db, regimen_id)
    if regimen is None:
        raise HTTPException(status_code=404, detail="regimen not found")
    patient = await patients_repo.get(db, regimen.patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="patient row missing")

    now = datetime.now(timezone.utc)
    # Stamp scheduled_at to a unique past second to avoid the unique
    # constraint clashing with a real 09:00/20:00 occurrence.
    adherence = await adherence_events_repo.create_scheduled(
        db,
        patient_id=regimen.patient_id,
        regimen_id=regimen.id,
        scheduled_at=now,
    )
    row = await scheduled_events_repo.enqueue(
        db,
        event_type="dose_due",
        patient_id=patient.phone,
        payload={
            "adherence_event_id": adherence.id,
            "regimen_id": regimen.id,
            "patient_db_id": regimen.patient_id,
            "medication_name": regimen.medication_name,
            "dose": regimen.dose,
            "scheduled_at_iso": now.isoformat(),
            "test_trigger": True,
        },
        scheduled_for=now,
    )
    await db.commit()
    return TestDoseResponse(
        scheduled_event_id=row.id,
        adherence_event_id=adherence.id,
        scheduled_for=row.scheduled_for,
        note="scheduler will pick this up on next tick (within SCHEDULER_POLL_SECONDS)",
    )


class TestRefillResponse(BaseModel):
    scheduled_event_id: int
    scheduled_for: datetime
    days_left: int | None
    note: str


@router.post(
    "/regimens/{regimen_id}/test-refill",
    response_model=TestRefillResponse,
)
async def fire_test_refill(
    regimen_id: int,
    db: AsyncSession = Depends(get_session),
) -> TestRefillResponse:
    """Enqueue a single refill reminder due NOW for this regimen — useful
    for demos. Requires the regimen to have supply_days_initial +
    supply_started_on set."""
    regimen = await regimens_repo.get(db, regimen_id)
    if regimen is None:
        raise HTTPException(status_code=404, detail="regimen not found")
    if regimen.supply_days_initial is None or regimen.supply_started_on is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "regimen has no supply tracking — set supply_days_initial "
                "and supply_started_on first"
            ),
        )
    patient = await patients_repo.get(db, regimen.patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="patient row missing")

    from services.scheduler.refill_reminders import supply_runs_out

    expiry = supply_runs_out(regimen)
    days_left = (expiry - datetime.now(timezone.utc).date()).days if expiry else None
    now = datetime.now(timezone.utc)
    row = await scheduled_events_repo.enqueue(
        db,
        event_type="refill_due",
        patient_id=patient.phone,
        payload={
            "regimen_id": regimen.id,
            "patient_db_id": regimen.patient_id,
            "medication_name": regimen.medication_name,
            "dose": regimen.dose,
            "stage": "test",
            "days_left": days_left,
            "supply_runs_out_iso": expiry.isoformat() if expiry else None,
            "cycle_key": regimen.supply_started_on.isoformat(),
            "test_trigger": True,
        },
        scheduled_for=now,
    )
    await db.commit()
    return TestRefillResponse(
        scheduled_event_id=row.id,
        scheduled_for=row.scheduled_for,
        days_left=days_left,
        note="scheduler will pick this up on next tick",
    )

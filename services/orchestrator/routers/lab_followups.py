"""Lab follow-up endpoints.

Extracted from main.py. CRUD + clinician completion/review + a demo
test-reminder trigger. ``LabFollowupDTO`` / ``_lab_to_dto`` come from the shared
routers._dtos module (also used by pre-visit + patient-detail).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import lab_followups as lab_followups_repo
from app.db.repositories import patients as patients_repo
from app.db.repositories import scheduled_events as scheduled_events_repo
from app.db.session import get_session
from services.orchestrator.routers._dtos import LabFollowupDTO, _lab_to_dto
from services.scheduler import lab_followups as lab_followups_scheduler

log = logging.getLogger(__name__)

router = APIRouter()


class LabFollowupCreateRequest(BaseModel):
    test_name: str = Field(min_length=1, max_length=255)
    due_by: date | None = None
    notes: str | None = None


@router.post("/patients/{patient_id}/lab-followups", response_model=LabFollowupDTO)
async def create_lab_followup(
    patient_id: int,
    payload: LabFollowupCreateRequest,
    db: AsyncSession = Depends(get_session),
) -> LabFollowupDTO:
    """Create a lab follow-up. When ``due_by`` is provided, the scheduler
    will materialize T-7 / T-1 / T+2 (overdue) reminders on its next pass."""
    patient = await patients_repo.get(db, patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="patient not found")
    row = await lab_followups_repo.create(
        db,
        patient_id=patient_id,
        test_name=payload.test_name,
        due_by=payload.due_by,
        notes=payload.notes,
    )
    # Eagerly materialize so the patient gets reminders without waiting
    # for the periodic loop (relevant when due_by is close).
    if patient.phone and row.due_by is not None:
        try:
            await lab_followups_scheduler.materialize_for_lab_followup(
                db, row, patient_phone=patient.phone
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "immediate lab materialize failed for lab=%s", row.id
            )
    await db.commit()
    return _lab_to_dto(row)


@router.get(
    "/patients/{patient_id}/lab-followups",
    response_model=list[LabFollowupDTO],
)
async def list_patient_lab_followups(
    patient_id: int,
    db: AsyncSession = Depends(get_session),
) -> list[LabFollowupDTO]:
    rows = await lab_followups_repo.list_for_patient(db, patient_id)
    return [_lab_to_dto(r) for r in rows]


@router.post(
    "/lab-followups/{lab_id}/mark-completed",
    response_model=LabFollowupDTO,
)
async def mark_lab_followup_completed(
    lab_id: int,
    db: AsyncSession = Depends(get_session),
) -> LabFollowupDTO:
    """Clinician-side completion (e.g., they confirmed the patient went)."""
    row = await lab_followups_repo.mark_completed(db, lab_id)
    if row is None:
        raise HTTPException(status_code=404, detail="lab follow-up not found")
    await lab_followups_scheduler.cancel_for_lab_followup(
        db, lab_followup_id=lab_id, reason="lab_completed_by_clinician"
    )
    await db.commit()
    return _lab_to_dto(row)


@router.post(
    "/lab-followups/{lab_id}/mark-reviewed",
    response_model=LabFollowupDTO,
)
async def mark_lab_followup_reviewed(
    lab_id: int,
    db: AsyncSession = Depends(get_session),
) -> LabFollowupDTO:
    """Final close: clinician reviewed the lab results."""
    row = await lab_followups_repo.mark_reviewed(db, lab_id)
    if row is None:
        raise HTTPException(status_code=404, detail="lab follow-up not found")
    await lab_followups_scheduler.cancel_for_lab_followup(
        db, lab_followup_id=lab_id, reason="lab_reviewed"
    )
    await db.commit()
    return _lab_to_dto(row)


class TestLabReminderResponse(BaseModel):
    scheduled_event_id: int
    scheduled_for: datetime
    stage: str
    note: str


@router.post(
    "/lab-followups/{lab_id}/test-reminder",
    response_model=TestLabReminderResponse,
)
async def fire_test_lab_reminder(
    lab_id: int,
    db: AsyncSession = Depends(get_session),
) -> TestLabReminderResponse:
    """Enqueue a single lab_followup_due event due NOW for demo purposes."""
    lab = await lab_followups_repo.get(db, lab_id)
    if lab is None:
        raise HTTPException(status_code=404, detail="lab follow-up not found")
    patient = await patients_repo.get(db, lab.patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="patient row missing")

    now = datetime.now(timezone.utc)
    # Stage label depends on current schedule context. For a manual demo
    # trigger we use "test" so it's clearly distinguishable in logs and
    # the dispatcher's status-aware copy still applies.
    row = await scheduled_events_repo.enqueue(
        db,
        event_type="lab_followup_due",
        patient_id=patient.phone,
        payload={
            "lab_followup_id": lab.id,
            "patient_db_id": lab.patient_id,
            "test_name": lab.test_name,
            "due_by_iso": lab.due_by.isoformat() if lab.due_by else None,
            "stage": "test",
            "test_trigger": True,
        },
        scheduled_for=now,
    )
    await db.commit()
    return TestLabReminderResponse(
        scheduled_event_id=row.id,
        scheduled_for=row.scheduled_for,
        stage="test",
        note="scheduler will pick this up on next tick",
    )

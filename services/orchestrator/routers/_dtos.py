"""Shared DTOs used across multiple orchestrator routers.

``LabFollowupDTO`` / ``RegimenDTO`` (+ their ``_*_to_dto`` helpers) are
referenced by several domains — the lab + regimen routers themselves, plus the
pre-visit summary and patient-detail aggregate views. They live here (rather
than in any one router) so every consumer imports the same definition without a
cross-router dependency.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from pydantic import BaseModel

from app.db.models import LabFollowup, Regimen


class LabFollowupDTO(BaseModel):
    id: int
    patient_id: int
    test_name: str
    status: str
    due_by: date | None
    notes: str | None
    booked_at: datetime | None
    completed_at: datetime | None
    reviewed_at: datetime | None
    days_until_due: int | None
    is_overdue: bool
    created_at: datetime
    updated_at: datetime


def _lab_to_dto(row: LabFollowup) -> LabFollowupDTO:
    today = datetime.now(timezone.utc).date()
    days_until_due: int | None = None
    if row.due_by is not None:
        days_until_due = (row.due_by - today).days
    is_overdue = (
        row.due_by is not None
        and row.due_by < today
        and row.status.value in {"due", "booked"}
    )
    return LabFollowupDTO(
        id=row.id,
        patient_id=row.patient_id,
        test_name=row.test_name,
        status=row.status.value,
        due_by=row.due_by,
        notes=row.notes,
        booked_at=row.booked_at,
        completed_at=row.completed_at,
        reviewed_at=row.reviewed_at,
        days_until_due=days_until_due,
        is_overdue=is_overdue,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class AppointmentDTO(BaseModel):
    """Shared by the doctor/appointments router (get_appointment) and the
    pre-visit summary aggregate. Constructed inline at each call site (no
    _to_dto helper) since the joins differ slightly."""

    id: int
    patient_id: int
    doctor_id: int
    doctor_name: str | None = None
    doctor_timezone: str | None = None
    patient_full_name: str | None = None
    scheduled_for: datetime
    end_at: datetime
    status: str
    summary: str | None = None
    notes: str | None = None
    calendar_html_link: str | None = None


class RegimenDTO(BaseModel):
    id: int
    patient_id: int
    medication_name: str
    dose: str
    schedule: dict[str, Any]
    starts_on: date | None
    ends_on: date | None
    strict_timing: bool
    supply_days_initial: int | None
    supply_started_on: date | None
    days_of_supply_remaining: int | None  # computed; null when supply not tracked
    created_at: datetime
    updated_at: datetime


def _days_of_supply_remaining(row: Regimen) -> int | None:
    if row.supply_days_initial is None or row.supply_started_on is None:
        return None
    today = datetime.now(timezone.utc).date()
    elapsed = (today - row.supply_started_on).days
    return max(0, row.supply_days_initial - elapsed)


def _regimen_to_dto(row: Regimen) -> RegimenDTO:
    return RegimenDTO(
        id=row.id,
        patient_id=row.patient_id,
        medication_name=row.medication_name,
        dose=row.dose,
        schedule=row.schedule or {},
        starts_on=row.starts_on,
        ends_on=row.ends_on,
        strict_timing=row.strict_timing,
        supply_days_initial=row.supply_days_initial,
        supply_started_on=row.supply_started_on,
        days_of_supply_remaining=_days_of_supply_remaining(row),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )

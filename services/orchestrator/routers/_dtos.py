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


class PatientSummaryDTO(BaseModel):
    """Lightweight row for the patients list page.

    Shared by the /patients listing, the patient-detail header, and the
    pre-visit brief's patient card — all three reuse the same flat shape
    so the ops console can render one header component everywhere.
    """

    id: int
    full_name: str
    phone: str
    cohort_diabetes: bool
    cohort_cardiac: bool
    cohort_fall_risk: bool
    active_regimen_count: int
    upcoming_appointment_count: int
    open_ticket_count: int
    created_at: datetime


class AdherenceSummaryDTO(BaseModel):
    window_days: int
    total: int
    taken: int            # all takens (on-time + late)
    taken_on_time: int    # tapped Taken inside the grace window
    taken_late: int       # tapped Mark-as-taken after sweep marked missed
    missed: int
    skipped: int
    delayed: int
    scheduled: int
    adherence_rate: float  # taken / (taken + missed + skipped); 0..1
    on_time_rate: float    # taken_on_time / taken; 0..1 (NaN-safe → 0.0)


def _adherence_summary(
    events: list[Any], window_days: int = 30
) -> AdherenceSummaryDTO:
    """Roll a list of adherence events up into the summary DTO.

    Duck-typed over the event rows (reads ``.status`` + optional
    ``.confirmation_metadata``) so both the pre-visit brief and the
    patient-detail aggregate can share it without importing the model.
    """
    counts = {"taken": 0, "missed": 0, "skipped": 0, "delayed": 0, "scheduled": 0}
    taken_late = 0
    for e in events:
        v = e.status.value if hasattr(e.status, "value") else str(e.status)
        if v in counts:
            counts[v] += 1
        if v == "taken":
            metadata = getattr(e, "confirmation_metadata", None) or {}
            if metadata.get("late_confirmed"):
                taken_late += 1
    completed = counts["taken"] + counts["missed"] + counts["skipped"]
    rate = (counts["taken"] / completed) if completed else 0.0
    on_time = counts["taken"] - taken_late
    on_time_rate = (on_time / counts["taken"]) if counts["taken"] else 0.0
    return AdherenceSummaryDTO(
        window_days=window_days,
        total=len(events),
        taken=counts["taken"],
        taken_on_time=on_time,
        taken_late=taken_late,
        missed=counts["missed"],
        skipped=counts["skipped"],
        delayed=counts["delayed"],
        scheduled=counts["scheduled"],
        adherence_rate=round(rate, 3),
        on_time_rate=round(on_time_rate, 3),
    )

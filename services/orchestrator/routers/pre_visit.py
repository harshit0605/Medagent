"""Pre-visit summary (doctor brief) endpoint. Extracted from main.py.

A single read-only aggregate — ``GET /appointments/{id}/pre-visit`` — that
pulls the patient's regimens, 30-day adherence, recent classified inbox
messages, open lab followups + ops tickets, cohort tags/flags, active
care-plan exemptions, and the prior visit's recap excerpt into one payload
so the doctor walks in informed. ``AppointmentDTO`` / ``PatientSummaryDTO`` /
``AdherenceSummaryDTO`` / ``RegimenDTO`` / ``LabFollowupDTO`` (and their
``_*_to_dto`` / ``_adherence_summary`` helpers) come from the shared
routers._dtos module.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AppointmentStatus, RecapStatus
from app.db.repositories import adherence_events as adherence_events_repo
from app.db.repositories import appointment_recaps as appointment_recaps_repo
from app.db.repositories import appointments as appointments_repo
from app.db.repositories import (
    care_plan_exemptions as care_plan_exemptions_repo,
)
from app.db.repositories import care_plans as care_plans_repo
from app.db.repositories import caregivers as caregivers_repo
from app.db.repositories import cohort_tags as cohort_tags_repo
from app.db.repositories import doctors as doctors_repo
from app.db.repositories import (
    inbound_classifications as inbound_classifications_repo,
)
from app.db.repositories import lab_followups as lab_followups_repo
from app.db.repositories import ops_tickets as ops_tickets_repo
from app.db.repositories import patients as patients_repo
from app.db.repositories import regimens as regimens_repo
from app.db.session import get_session
from services.orchestrator.routers._dtos import (
    AdherenceSummaryDTO,
    AppointmentDTO,
    LabFollowupDTO,
    PatientSummaryDTO,
    RegimenDTO,  # noqa: F401 — resolves the list["RegimenDTO"] forward ref
    _adherence_summary,
    _lab_to_dto,
    _regimen_to_dto,
)

router = APIRouter()


# ---- Pre-visit summary (doctor brief) -------------------------------------


class PreVisitInboxItemDTO(BaseModel):
    id: int
    created_at: datetime
    category: str
    urgency: str
    summary: str | None
    inbound_text: str | None
    handler_used: str | None
    escalated: bool


class PreVisitTicketDTO(BaseModel):
    ticket_id: str
    category: str
    priority: str
    status: str
    is_overdue: bool
    is_snoozed: bool
    created_at: datetime
    # First-cross SLA breach marker — surfaced on the doctor's
    # pre-visit brief so they can see "this care issue went past
    # SLA before it was resolved" at a glance.
    sla_breached_at: datetime | None = None


class PreVisitCohortTagDTO(BaseModel):
    cohort_tag_id: int
    label: str
    slug: str


class PreVisitExemptionDTO(BaseModel):
    id: int
    care_plan_id: int
    care_plan_test_name: str | None
    reason: str
    expires_at: datetime | None


class PreVisitRecapExcerptDTO(BaseModel):
    """Just enough of the most recent SENT recap so the doctor can
    skim what the previous visit landed on. Detail is one click away
    via /appointments/{id}/recap."""

    appointment_id: int
    appointment_date: datetime
    summary: str  # generated_text trimmed to ~600 chars
    status: str  # sent / acknowledged / questioned


class PreVisitSummaryDTO(BaseModel):
    appointment: AppointmentDTO
    patient: PatientSummaryDTO
    cohort_flags: list[str]  # legacy boolean cohorts the patient is in
    cohort_tags: list[PreVisitCohortTagDTO]
    regimens: list["RegimenDTO"]
    adherence: AdherenceSummaryDTO
    open_lab_followups: list[LabFollowupDTO]
    open_tickets: list[PreVisitTicketDTO]
    active_exemptions: list[PreVisitExemptionDTO]
    recent_inbox: list[PreVisitInboxItemDTO]
    last_recap: PreVisitRecapExcerptDTO | None
    has_caregiver_cc: bool


@router.get(
    "/appointments/{appointment_id}/pre-visit",
    response_model=PreVisitSummaryDTO,
)
async def get_pre_visit_summary(
    appointment_id: int, db: AsyncSession = Depends(get_session)
) -> PreVisitSummaryDTO:
    """Doctor-facing pre-visit brief. Aggregates the patient's regimens,
    30-day adherence, recent classified inbox messages, open lab
    followups, open ops tickets, cohort tags + flags, active care-plan
    exemptions, and the most recent prior visit's recap excerpt — so
    the doctor walks in informed without clicking through five
    different ops console screens.

    Read-only; no clinical decisions surfaced — just the data."""
    appointment = await appointments_repo.get(db, appointment_id)
    if appointment is None:
        raise HTTPException(status_code=404, detail="appointment not found")

    doctor = await doctors_repo.get(db, appointment.doctor_id)
    patient = await patients_repo.get(db, appointment.patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="patient row missing")

    appointment_dto = AppointmentDTO(
        id=appointment.id,
        patient_id=appointment.patient_id,
        doctor_id=appointment.doctor_id,
        doctor_name=doctor.name if doctor else None,
        doctor_timezone=doctor.timezone if doctor else None,
        patient_full_name=patient.full_name,
        scheduled_for=appointment.scheduled_for,
        end_at=appointment.end_at,
        status=appointment.status.value,
        summary=appointment.summary,
        notes=appointment.notes,
        calendar_html_link=appointment.calendar_html_link,
    )

    # Regimens — fetched once, reused for both the regimen list AND
    # the patient_summary header's active_regimen_count.
    today = datetime.now(timezone.utc).date()
    active_regimens = await regimens_repo.list_for_patient(
        db, patient.id, active_on=today
    )
    regimen_dtos = [_regimen_to_dto(r) for r in active_regimens]

    # Patient summary card — minimal flat shape matching the /patients
    # listing so the page can reuse the standard header component.
    upcoming_appts = await appointments_repo.list_for_patient(
        db, patient.id, upcoming_only=True, limit=5
    )
    upcoming_confirmed = [
        a
        for a in upcoming_appts
        if a.status == AppointmentStatus.confirmed
    ]
    # Patient's ops tickets — fetched ONCE (by phone, the id ops_tickets
    # uses) and reused for both the summary count here and the detailed
    # open-tickets list below.
    all_tickets = (
        await ops_tickets_repo.list_for_patient(db, patient.phone)
        if patient.phone
        else []
    )
    open_ticket_count = sum(
        1 for t in all_tickets if t.status.value == "open"
    )
    patient_summary_dto = PatientSummaryDTO(
        id=patient.id,
        full_name=patient.full_name,
        phone=patient.phone,
        cohort_diabetes=patient.cohort_diabetes,
        cohort_cardiac=patient.cohort_cardiac,
        cohort_fall_risk=patient.cohort_fall_risk,
        active_regimen_count=len(active_regimens),
        upcoming_appointment_count=len(upcoming_confirmed),
        open_ticket_count=open_ticket_count,
        created_at=patient.created_at,
    )

    # 30-day adherence.
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    adh_events = await adherence_events_repo.list_for_patient(
        db, patient.id, since=cutoff
    )
    adherence_dto = _adherence_summary(adh_events, window_days=30)

    # Open lab followups.
    all_labs = await lab_followups_repo.list_for_patient(db, patient.id)
    open_labs = [
        _lab_to_dto(lab)
        for lab in all_labs
        if lab.status.value in ("due", "booked")
    ]

    # Open ops tickets — reuse the rows already fetched for the count above.
    open_tickets_rows = all_tickets
    now = datetime.now(timezone.utc)
    open_tickets: list[PreVisitTicketDTO] = []
    for t in open_tickets_rows:
        if t.status.value not in ("open", "acknowledged"):
            continue
        created = t.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        snoozed_until = t.snoozed_until
        if snoozed_until is not None and snoozed_until.tzinfo is None:
            snoozed_until = snoozed_until.replace(tzinfo=timezone.utc)
        is_snoozed = snoozed_until is not None and snoozed_until > now
        is_overdue = (
            not is_snoozed
            and (created + timedelta(minutes=t.sla_minutes)) <= now
        )
        sla_breached_at = t.sla_breached_at
        if sla_breached_at is not None and sla_breached_at.tzinfo is None:
            sla_breached_at = sla_breached_at.replace(tzinfo=timezone.utc)
        open_tickets.append(
            PreVisitTicketDTO(
                ticket_id=str(t.id),
                category=t.category,
                priority=t.priority,
                status=t.status.value,
                is_overdue=is_overdue,
                is_snoozed=is_snoozed,
                created_at=t.created_at,
                sla_breached_at=sla_breached_at,
            )
        )

    # Active care-plan exemptions (joined with plan info for the test name).
    exemptions_with_plan = (
        await care_plan_exemptions_repo.list_with_plan_info(
            db, patient.id, include_inactive=False
        )
    )
    exemption_dtos = [
        PreVisitExemptionDTO(
            id=ex.id,
            care_plan_id=ex.care_plan_id,
            care_plan_test_name=plan.test_name if plan else None,
            reason=ex.reason,
            expires_at=ex.expires_at,
        )
        for ex, plan in exemptions_with_plan
    ]

    # Cohort tags.
    tag_assignments = await cohort_tags_repo.list_for_patient(db, patient.id)
    tag_dtos = [
        PreVisitCohortTagDTO(
            cohort_tag_id=tag.id, label=tag.label, slug=tag.slug
        )
        for _assignment, tag in tag_assignments
    ]
    cohort_flags = [
        attr
        for attr in care_plans_repo.KNOWN_COHORT_ATTRS
        if getattr(patient, attr, False)
    ]

    # Recent inbox — last 10 freeform classifications, newest first.
    if patient.phone:
        inbox_rows = await inbound_classifications_repo.list_recent(
            db, patient_phone=patient.phone, limit=10
        )
    else:
        inbox_rows = []
    inbox_dtos = [
        PreVisitInboxItemDTO(
            id=r.id,
            created_at=r.created_at,
            category=r.category,
            urgency=r.urgency,
            summary=r.summary,
            inbound_text=r.inbound_text,
            handler_used=r.handler_used,
            escalated=r.escalated,
        )
        for r in inbox_rows
    ]

    # Most recent SENT recap on a different appointment (doctor wants
    # the PRIOR visit's recap, not this appointment's recap-in-progress).
    last_recap_dto: PreVisitRecapExcerptDTO | None = None
    prior_recaps = await appointment_recaps_repo.list_for_patient(
        db, patient.id, limit=20
    )
    for recap in prior_recaps:
        if recap.appointment_id == appointment.id:
            continue
        if recap.status == RecapStatus.draft:
            continue
        if not recap.generated_text:
            continue
        prior_appt = await appointments_repo.get(db, recap.appointment_id)
        if prior_appt is None:
            continue
        excerpt = recap.generated_text.strip()
        if len(excerpt) > 600:
            excerpt = excerpt[:597].rstrip() + "…"
        last_recap_dto = PreVisitRecapExcerptDTO(
            appointment_id=recap.appointment_id,
            appointment_date=prior_appt.scheduled_for,
            summary=excerpt,
            status=recap.status.value,
        )
        break

    # Caregiver cc state — bool flag for the header (full list is on
    # patient detail).
    caregivers = await caregivers_repo.list_active_recap_recipients(
        db, patient.id
    )
    has_caregiver_cc = len(caregivers) > 0

    return PreVisitSummaryDTO(
        appointment=appointment_dto,
        patient=patient_summary_dto,
        cohort_flags=cohort_flags,
        cohort_tags=tag_dtos,
        regimens=regimen_dtos,
        adherence=adherence_dto,
        open_lab_followups=open_labs,
        open_tickets=open_tickets,
        active_exemptions=exemption_dtos,
        recent_inbox=inbox_dtos,
        last_recap=last_recap_dto,
        has_caregiver_cc=has_caregiver_cc,
    )


# Resolve the list["RegimenDTO"] forward ref now that RegimenDTO is in scope.
PreVisitSummaryDTO.model_rebuild()

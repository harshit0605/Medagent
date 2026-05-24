"""Doctor + Google-Calendar + appointment endpoints. Extracted from main.py.

Doctor CRUD + OAuth token exchange, on-call toggle, the daily digest, calendar
availability/booking/cancel (via the gcal adapter), and the single-appointment
read. ``AppointmentDTO`` comes from the shared routers._dtos module
(pre-visit summary embeds it too).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Doctor, DoctorOAuthStatus
from app.db.repositories import appointments as appointments_repo
from app.db.repositories import doctors as doctors_repo
from app.db.repositories import patients as patients_repo
from app.db.session import get_session
from services.orchestrator import google_calendar as gcal
from services.orchestrator.routers._dtos import AppointmentDTO

log = logging.getLogger(__name__)

router = APIRouter()


# ---- Doctor / Google Calendar endpoints ------------------------------------


class DoctorCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    email: str = Field(min_length=3, max_length=255)
    phone: str | None = None
    timezone: str = Field(default="UTC", max_length=64)
    calendar_id: str = Field(default="primary", max_length=255)


class DoctorDTO(BaseModel):
    id: int
    name: str
    email: str
    phone: str | None = None
    timezone: str
    calendar_id: str
    oauth_status: str
    google_user_id: str | None = None
    # Inbound calendar-sync state. ``gcal_last_synced_at`` is the
    # wall-clock of the most recent successful incremental pass;
    # the UI renders "synced N min ago" off this so ops sees the
    # sweep is working without querying logs.
    gcal_last_synced_at: datetime | None = None
    is_on_call: bool = False
    created_at: datetime
    updated_at: datetime


def _doctor_to_dto(row: Doctor) -> DoctorDTO:
    last_synced = getattr(row, "gcal_last_synced_at", None)
    if last_synced is not None and last_synced.tzinfo is None:
        last_synced = last_synced.replace(tzinfo=timezone.utc)
    return DoctorDTO(
        id=row.id,
        name=row.name,
        email=row.email,
        phone=row.phone,
        timezone=row.timezone,
        calendar_id=row.calendar_id,
        oauth_status=row.oauth_status.value,
        google_user_id=row.google_user_id,
        gcal_last_synced_at=last_synced,
        is_on_call=bool(getattr(row, "is_on_call", False)),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.post("/doctors", response_model=DoctorDTO)
async def create_doctor(
    payload: DoctorCreateRequest, db: AsyncSession = Depends(get_session)
) -> DoctorDTO:
    existing = await doctors_repo.get_by_email(db, payload.email)
    if existing is not None:
        raise HTTPException(status_code=409, detail="doctor with this email already exists")
    row = await doctors_repo.create(
        db,
        name=payload.name,
        email=payload.email,
        phone=payload.phone,
        timezone_name=payload.timezone,
        calendar_id=payload.calendar_id,
    )
    return _doctor_to_dto(row)


@router.get("/doctors", response_model=list[DoctorDTO])
async def list_doctors(db: AsyncSession = Depends(get_session)) -> list[DoctorDTO]:
    rows = await doctors_repo.list_all(db)
    return [_doctor_to_dto(r) for r in rows]


@router.get("/doctors/{doctor_id}", response_model=DoctorDTO)
async def get_doctor(
    doctor_id: int, db: AsyncSession = Depends(get_session)
) -> DoctorDTO:
    row = await doctors_repo.get(db, doctor_id)
    if row is None:
        raise HTTPException(status_code=404, detail="doctor not found")
    return _doctor_to_dto(row)


class DoctorOnCallRequest(BaseModel):
    on_call: bool


@router.post(
    "/doctors/{doctor_id}/on-call", response_model=DoctorDTO
)
async def set_doctor_on_call(
    doctor_id: int,
    body: DoctorOnCallRequest,
    db: AsyncSession = Depends(get_session),
) -> DoctorDTO:
    """Toggle the doctor's ``is_on_call`` flag. Doctors flagged
    on-call appear in the critical-alert paging fallback when
    a patient has no primary-doctor history. A doctor without
    a ``phone`` can still be flagged but won't actually
    receive pages — surfaced as a UI warning rather than
    rejected here, since the flag is also used by future
    rota-aware features."""
    row = await doctors_repo.set_on_call(
        db, doctor_id, on_call=body.on_call
    )
    if row is None:
        raise HTTPException(status_code=404, detail="doctor not found")
    # Build the DTO BEFORE commit. SQLAlchemy expires all row
    # attributes after commit by default; accessing them then
    # triggers a lazy reload that races connection-cleanup on
    # async sessions and surfaces as MissingGreenlet errors in
    # tests. Building from the still-attached row sidesteps it.
    dto = _doctor_to_dto(row)
    await db.commit()
    return dto


@router.post("/doctors/{doctor_id}/disconnect", response_model=DoctorDTO)
async def disconnect_doctor(
    doctor_id: int, db: AsyncSession = Depends(get_session)
) -> DoctorDTO:
    row = await doctors_repo.mark_disconnected(
        db, doctor_id, status=DoctorOAuthStatus.disconnected
    )
    if row is None:
        raise HTTPException(status_code=404, detail="doctor not found")
    return _doctor_to_dto(row)


class OAuthCallbackRequest(BaseModel):
    """Posted by the Next.js OAuth callback after exchanging code → tokens."""

    code: str = Field(min_length=1)
    redirect_uri: str = Field(min_length=1)


@router.post("/doctors/{doctor_id}/oauth/callback", response_model=DoctorDTO)
async def doctor_oauth_callback(
    doctor_id: int,
    payload: OAuthCallbackRequest,
    db: AsyncSession = Depends(get_session),
) -> DoctorDTO:
    """Internal endpoint — Next.js calls this with the freshly issued auth code.

    We exchange it server-to-server (so the client secret never touches the
    browser), then persist the encrypted refresh token + cached access token.
    """
    row = await doctors_repo.get(db, doctor_id)
    if row is None:
        raise HTTPException(status_code=404, detail="doctor not found")
    try:
        token_payload = await gcal.exchange_code_for_tokens(
            code=payload.code, redirect_uri=payload.redirect_uri
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"google token exchange failed: {exc}") from exc

    refresh_token = token_payload.get("refresh_token")
    if not refresh_token:
        # Google only issues a refresh_token on the first consent. If we didn't
        # get one, the user previously authorized this client without
        # access_type=offline / prompt=consent.
        raise HTTPException(
            status_code=400,
            detail=(
                "Google did not return a refresh_token. Visit "
                "https://myaccount.google.com/permissions, revoke this app, "
                "then run Connect again."
            ),
        )

    access_token = token_payload["access_token"]
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=int(token_payload.get("expires_in", 3600))
    )
    scopes = token_payload.get("scope", "")

    google_user_id: str | None = None
    id_token = token_payload.get("id_token")
    if id_token:
        # The id_token is a JWT — decode the middle segment to read `sub`.
        # No verification needed; we got it directly from Google over TLS.
        import base64
        import json as _json

        try:
            parts = id_token.split(".")
            padding = "=" * (-len(parts[1]) % 4)
            claims = _json.loads(base64.urlsafe_b64decode(parts[1] + padding))
            google_user_id = claims.get("sub")
        except Exception:
            log.warning("id_token claim parse failed; storing without google_user_id")

    updated = await doctors_repo.store_oauth_tokens(
        db,
        doctor_id,
        refresh_token=refresh_token,
        access_token=access_token,
        access_token_expires_at=expires_at,
        scopes=scopes,
        google_user_id=google_user_id,
    )
    assert updated is not None
    return _doctor_to_dto(updated)


# ---- Calendar tooling (availability / booking) -----------------------------


class TimeSlotDTO(BaseModel):
    start: datetime
    end: datetime


class AvailabilityDTO(BaseModel):
    doctor_id: int
    timezone: str
    requested_window: TimeSlotDTO
    duration_minutes: int
    busy: list[TimeSlotDTO]
    free: list[TimeSlotDTO]


class CalendarEventDTO(BaseModel):
    event_id: str
    calendar_id: str
    summary: str | None = None
    description: str | None = None
    start: datetime
    end: datetime
    html_link: str | None = None
    attendees: list[str] = Field(default_factory=list)
    status: str | None = None


# ---- Doctor daily digest ---------------------------------------------------


class DigestAppointmentDTO(BaseModel):
    appointment_id: int
    patient_id: int
    patient_full_name: str
    scheduled_for: datetime
    end_at: datetime
    status: str
    summary: str | None
    has_pending_recap: bool


class DigestRecapDraftDTO(BaseModel):
    recap_id: int
    appointment_id: int
    patient_id: int
    patient_full_name: str
    appointment_date: datetime
    created_at: datetime


class DigestTicketDTO(BaseModel):
    ticket_id: int
    patient_id: int | None
    patient_full_name: str | None
    patient_phone: str
    category: str
    priority: str
    status: str
    sla_minutes: int
    created_at: datetime
    sla_breached_at: datetime | None


class DigestSideEffectDTO(BaseModel):
    ticket_id: int
    patient_id: int | None
    patient_full_name: str | None
    created_at: datetime
    status: str
    reported_text: str | None


class DoctorDigestDTO(BaseModel):
    doctor_id: int
    doctor_name: str
    when: datetime
    summary_counts: dict[str, int]
    appointments_today: list[DigestAppointmentDTO]
    recap_drafts_pending: list[DigestRecapDraftDTO]
    side_effect_reports_24h: list[DigestSideEffectDTO]
    open_tickets: list[DigestTicketDTO]


@router.get(
    "/doctors/{doctor_id}/daily-digest",
    response_model=DoctorDigestDTO,
)
async def get_doctor_daily_digest(
    doctor_id: int,
    when: datetime | None = None,
    db: AsyncSession = Depends(get_session),
) -> DoctorDigestDTO:
    """Doctor's morning panel-management view. Aggregates today's
    appointments, pending recap drafts, recent side-effect
    reports, and open high-priority tickets for the doctor's
    panel into a single read-only DTO so the doctor can scan
    their day in 5 seconds.

    ``when`` defaults to "now in UTC". Pass an explicit tz-aware
    datetime to align the "today" window to a doctor's local
    timezone (most clinics will compute their local-day window
    client-side and pass UTC bounds in)."""
    from services.orchestrator import doctor_digest as digest_module

    doctor = await doctors_repo.get(db, doctor_id)
    if doctor is None:
        raise HTTPException(status_code=404, detail="doctor not found")

    digest = await digest_module.build_digest(
        db,
        doctor_id=doctor_id,
        doctor_name=doctor.name,
        when=when,
    )
    return DoctorDigestDTO(
        doctor_id=digest.doctor_id,
        doctor_name=digest.doctor_name,
        when=digest.when,
        summary_counts=digest.summary_counts,
        appointments_today=[
            DigestAppointmentDTO(
                appointment_id=a.appointment_id,
                patient_id=a.patient_id,
                patient_full_name=a.patient_full_name,
                scheduled_for=a.scheduled_for,
                end_at=a.end_at,
                status=a.status,
                summary=a.summary,
                has_pending_recap=a.has_pending_recap,
            )
            for a in digest.appointments_today
        ],
        recap_drafts_pending=[
            DigestRecapDraftDTO(
                recap_id=r.recap_id,
                appointment_id=r.appointment_id,
                patient_id=r.patient_id,
                patient_full_name=r.patient_full_name,
                appointment_date=r.appointment_date,
                created_at=r.created_at,
            )
            for r in digest.recap_drafts_pending
        ],
        side_effect_reports_24h=[
            DigestSideEffectDTO(
                ticket_id=s.ticket_id,
                patient_id=s.patient_id,
                patient_full_name=s.patient_full_name,
                created_at=s.created_at,
                status=s.status,
                reported_text=s.reported_text,
            )
            for s in digest.side_effect_reports_24h
        ],
        open_tickets=[
            DigestTicketDTO(
                ticket_id=t.ticket_id,
                patient_id=t.patient_id,
                patient_full_name=t.patient_full_name,
                patient_phone=t.patient_phone,
                category=t.category,
                priority=t.priority,
                status=t.status,
                sla_minutes=t.sla_minutes,
                created_at=t.created_at,
                sla_breached_at=t.sla_breached_at,
            )
            for t in digest.open_tickets
        ],
    )


@router.get("/doctors/{doctor_id}/availability", response_model=AvailabilityDTO)
async def get_doctor_availability(
    doctor_id: int,
    start: datetime,
    end: datetime,
    duration_minutes: int = 30,
    db: AsyncSession = Depends(get_session),
) -> AvailabilityDTO:
    if start >= end:
        raise HTTPException(status_code=400, detail="end must be after start")
    if duration_minutes < 5 or duration_minutes > 480:
        raise HTTPException(status_code=400, detail="duration_minutes must be 5..480")
    try:
        result = await gcal.find_slots(
            db,
            doctor_id=doctor_id,
            window=gcal.TimeSlot(start=start, end=end),
            duration_minutes=duration_minutes,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return AvailabilityDTO(
        doctor_id=result.doctor_id,
        timezone=result.timezone,
        requested_window=TimeSlotDTO(**result.requested_window.model_dump()),
        duration_minutes=result.duration_minutes,
        busy=[TimeSlotDTO(**b.model_dump()) for b in result.busy],
        free=[TimeSlotDTO(**f.model_dump()) for f in result.free],
    )


class BookAppointmentRequest(BaseModel):
    start: datetime
    end: datetime
    summary: str = Field(min_length=1, max_length=255)
    description: str | None = None
    patient_email: str | None = None
    patient_phone: str | None = None
    extra_attendees: list[str] = Field(default_factory=list)


@router.post(
    "/doctors/{doctor_id}/appointments",
    response_model=CalendarEventDTO,
)
async def book_doctor_appointment(
    doctor_id: int,
    payload: BookAppointmentRequest,
    db: AsyncSession = Depends(get_session),
) -> CalendarEventDTO:
    if payload.start >= payload.end:
        raise HTTPException(status_code=400, detail="end must be after start")
    try:
        event = await gcal.book_slot(
            db,
            doctor_id=doctor_id,
            start=payload.start,
            end=payload.end,
            summary=payload.summary,
            description=payload.description,
            patient_email=payload.patient_email,
            patient_phone=payload.patient_phone,
            attendees=payload.extra_attendees,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return CalendarEventDTO(**event.model_dump())


@router.delete("/doctors/{doctor_id}/appointments/{event_id}", status_code=204)
async def cancel_doctor_appointment(
    doctor_id: int,
    event_id: str,
    db: AsyncSession = Depends(get_session),
) -> None:
    try:
        await gcal.cancel_event(db, doctor_id=doctor_id, event_id=event_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/appointments/{appointment_id}", response_model=AppointmentDTO)
async def get_appointment(
    appointment_id: int, db: AsyncSession = Depends(get_session)
) -> AppointmentDTO:
    appointment = await appointments_repo.get(db, appointment_id)
    if appointment is None:
        raise HTTPException(status_code=404, detail="appointment not found")
    doctor = await doctors_repo.get(db, appointment.doctor_id)
    patient = await patients_repo.get(db, appointment.patient_id)
    return AppointmentDTO(
        id=appointment.id,
        patient_id=appointment.patient_id,
        doctor_id=appointment.doctor_id,
        doctor_name=doctor.name if doctor else None,
        doctor_timezone=doctor.timezone if doctor else None,
        patient_full_name=patient.full_name if patient else None,
        scheduled_for=appointment.scheduled_for,
        end_at=appointment.end_at,
        status=appointment.status.value,
        summary=appointment.summary,
        notes=appointment.notes,
        calendar_html_link=appointment.calendar_html_link,
    )

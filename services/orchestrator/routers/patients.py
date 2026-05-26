"""Patient list / detail / timeline + language, pause, erasure & export.

Extracted from main.py. Everything keyed off a single patient row:

* ``GET /patients`` — the ops-console listing (PatientSummaryDTO rows).
* ``GET /patients/{id}`` — the full detail aggregate (get_patient_detail),
  reused by the language / reset / pause / erase endpoints which all return
  the refreshed detail DTO.
* ``GET /patients/{id}/timeline`` — the unified chronological event stream.
* ``GET /i18n/languages`` + ``PUT /patients/{id}/language`` — language opts.
* bot pause/unpause, right-of-erasure, and the DSAR data export.

Shared row/summary DTOs (PatientSummaryDTO / AdherenceSummaryDTO /
RegimenDTO / LabFollowupDTO and their helpers) come from routers._dtos.
The Google-Calendar push webhook stays in main.py (auth-exempt) even
though it physically sat between these endpoints before extraction.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AppointmentStatus, OpsTicket
from app.db.repositories import adherence_events as adherence_events_repo
from app.db.repositories import appointments as appointments_repo
from app.db.repositories import doctors as doctors_repo
from app.db.repositories import lab_followups as lab_followups_repo
from app.db.repositories import ops_tickets as ops_tickets_repo
from app.db.repositories import patients as patients_repo
from app.db.repositories import regimens as regimens_repo
from app.db.repositories import scheduled_events as scheduled_events_repo
from app.db.session import get_session
from services.orchestrator.side_effect_handler import _extract_reported_text
from services.orchestrator.routers._dtos import (
    AdherenceSummaryDTO,
    LabFollowupDTO,
    PatientSummaryDTO,
    RegimenDTO,  # noqa: F401 — resolves the list["RegimenDTO"] forward ref
    _adherence_summary,
    _lab_to_dto,
    _regimen_to_dto,
)

log = logging.getLogger(__name__)

router = APIRouter()


# ---- Patient list + detail endpoints (powers the ops console) ---------------


class AdherenceEventDTO(BaseModel):
    id: int
    regimen_id: int | None
    medication_name: str | None
    scheduled_at: datetime
    status: str
    confirmed_at: datetime | None


class AppointmentSummaryDTO(BaseModel):
    id: int
    doctor_id: int
    doctor_name: str | None
    scheduled_for: datetime
    end_at: datetime
    status: str
    calendar_html_link: str | None


class RefillEventDTO(BaseModel):
    id: int
    regimen_id: int | None
    medication_name: str | None
    scheduled_for: datetime
    dispatched_at: datetime | None
    stage: str | None
    status: str        # raw scheduled_event status
    label: str         # human-friendly outcome


class SideEffectReportDTO(BaseModel):
    """One side_effect_report ticket the patient has filed. Surfaced
    on the patient detail page so a doctor can spot patterns across
    multiple events ("3 reports in 2 months — clearly the new
    medication"). The verbatim ``reported_text`` is extracted from
    the ticket notes by ``_extract_reported_text``; the rest are
    direct ticket fields."""

    ticket_id: str
    status: str
    priority: str
    created_at: datetime
    acknowledged_at: datetime | None = None
    resolved_at: datetime | None = None
    sla_breached_at: datetime | None = None
    reported_text: str | None = None


class PatientDetailDTO(BaseModel):
    id: int
    full_name: str
    phone: str
    consent_sms: bool
    consent_voice: bool
    consent_email: bool
    cohort_diabetes: bool
    cohort_cardiac: bool
    cohort_fall_risk: bool
    onboarding_step: str | None
    preferred_language: str
    # Ops-initiated bot pause. NULL on the timestamp = bot is live;
    # non-NULL = paused. The reason / by columns are informational
    # — the dispatcher gate keys off the timestamp alone.
    bot_paused_at: datetime | None = None
    bot_paused_reason: str | None = None
    bot_paused_by: str | None = None
    # Right-of-erasure state. When set, the row's PII has been
    # overwritten and the UI should render an "erased" banner +
    # suppress live-patient action affordances.
    erased_at: datetime | None = None
    consent_revoked_at: datetime | None = None
    consent_revoked_reason: str | None = None
    created_at: datetime
    updated_at: datetime
    regimens: list["RegimenDTO"]
    upcoming_appointments: list[AppointmentSummaryDTO]
    recent_adherence_events: list[AdherenceEventDTO]
    recent_refill_events: list[RefillEventDTO]
    lab_followups: list[LabFollowupDTO]
    adherence_summary: AdherenceSummaryDTO
    # Last 20 side_effect_report tickets newest-first. Patient-safety
    # signal — surfaced on the detail page so a doctor doesn't have
    # to drill into the ops queue per-ticket to see the history.
    recent_side_effect_reports: list[SideEffectReportDTO] = []


def _refill_event_label(status: str, error_field: str | None) -> str:
    """Map a refill_due ScheduledEvent's (status, error) tuple to a
    human-readable outcome the ops console timeline can display."""
    err = (error_field or "").lower()
    if status == "pending":
        return "scheduled"
    if status == "dispatched":
        return "reminder sent"
    if status == "skipped":
        if "refill_done" in err:
            return "patient refilled"
        if "refill_rescheduled" in err or "snooze" in err:
            return "snoozed"
        if "regimen_deactivated" in err:
            return "regimen ended"
        if err.startswith("not_applicable:cycle"):
            return "stale cycle"
        if err.startswith("stale:"):
            return "stale (too late)"
        return f"skipped: {error_field}" if error_field else "skipped"
    if status == "failed":
        return f"failed: {error_field}" if error_field else "failed"
    return status


@router.get("/patients", response_model=list[PatientSummaryDTO])
async def list_patients(
    db: AsyncSession = Depends(get_session), limit: int = 200
) -> list[PatientSummaryDTO]:

    rows = await patients_repo.list_all(db, limit=limit)
    today = datetime.now(timezone.utc).date()
    # Open-ticket counts for ALL patients in one grouped query (keyed by the
    # phone the ticket carries) — previously this re-fetched every open ticket
    # org-wide inside the per-patient loop (an N+1 full-scan storm).
    open_ticket_counts = await ops_tickets_repo.open_counts_by_patient(db)
    out: list[PatientSummaryDTO] = []
    for p in rows:
        regimens = await regimens_repo.list_for_patient(db, p.id, active_on=today)
        appts_raw = await appointments_repo.list_for_patient(
            db, p.id, upcoming_only=True, limit=5
        )
        # Only confirmed upcoming — cancelled / no_show shouldn't inflate the count.
        appts = [a for a in appts_raw if a.status == AppointmentStatus.confirmed]
        out.append(
            PatientSummaryDTO(
                id=p.id,
                full_name=p.full_name,
                phone=p.phone,
                cohort_diabetes=p.cohort_diabetes,
                cohort_cardiac=p.cohort_cardiac,
                cohort_fall_risk=p.cohort_fall_risk,
                active_regimen_count=len(regimens),
                upcoming_appointment_count=len(appts),
                open_ticket_count=open_ticket_counts.get(p.phone, 0),
                created_at=p.created_at,
            )
        )
    return out


@router.get("/patients/{patient_id}", response_model=PatientDetailDTO)
async def get_patient_detail(
    patient_id: int, db: AsyncSession = Depends(get_session)
) -> PatientDetailDTO:
    p = await patients_repo.get(db, patient_id)
    if p is None:
        raise HTTPException(status_code=404, detail="patient not found")

    regimens = await regimens_repo.list_for_patient(db, patient_id)


    appts_raw = await appointments_repo.list_for_patient(
        db, patient_id, upcoming_only=True, limit=10
    )
    appts = [a for a in appts_raw if a.status == AppointmentStatus.confirmed]
    appt_dtos: list[AppointmentSummaryDTO] = []
    doctor_cache: dict[int, str] = {}
    for a in appts:
        if a.doctor_id not in doctor_cache:
            doc = await doctors_repo.get(db, a.doctor_id)
            doctor_cache[a.doctor_id] = doc.name if doc else f"Doctor #{a.doctor_id}"
        appt_dtos.append(
            AppointmentSummaryDTO(
                id=a.id,
                doctor_id=a.doctor_id,
                doctor_name=doctor_cache[a.doctor_id],
                scheduled_for=a.scheduled_for,
                end_at=a.end_at,
                status=a.status.value,
                calendar_html_link=a.calendar_html_link,
            )
        )

    since = datetime.now(timezone.utc) - timedelta(days=30)
    until = datetime.now(timezone.utc) + timedelta(days=2)
    adh_events = await adherence_events_repo.list_for_patient(
        db, patient_id, since=since, until=until, limit=200
    )
    # Resolve medication name per regimen for the timeline display.
    regimen_med: dict[int, str] = {r.id: r.medication_name for r in regimens}
    adh_dtos = [
        AdherenceEventDTO(
            id=e.id,
            regimen_id=e.regimen_id,
            medication_name=regimen_med.get(e.regimen_id) if e.regimen_id else None,
            scheduled_at=e.scheduled_at,
            status=e.status.value,
            confirmed_at=e.confirmed_at,
        )
        for e in adh_events
    ]
    summary = _adherence_summary(adh_events, window_days=30)

    # Recent refill_due ScheduledEvents for this patient. Filter on
    # patient.phone (the wa_id used as ScheduledEvent.patient_id) and
    # post-filter regimen_id ∈ this patient's regimens to be safe — phones
    # can theoretically be reused across patient rows.
    refill_rows = await scheduled_events_repo.list_recent(
        db, patient_id=p.phone, limit=200
    )
    patient_regimen_ids = {r.id for r in regimens}
    refill_dtos: list[RefillEventDTO] = []
    for ev in refill_rows:
        if ev.event_type != "refill_due":
            continue
        payload = ev.payload or {}
        regimen_id = payload.get("regimen_id")
        if regimen_id is not None and int(regimen_id) not in patient_regimen_ids:
            continue
        refill_dtos.append(
            RefillEventDTO(
                id=ev.id,
                regimen_id=regimen_id,
                medication_name=regimen_med.get(int(regimen_id))
                if regimen_id is not None
                else None,
                scheduled_for=ev.scheduled_for,
                dispatched_at=ev.dispatched_at,
                stage=payload.get("stage"),
                status=ev.status.value,
                label=_refill_event_label(ev.status.value, ev.error),
            )
        )
    # Newest first; cap to 30 in the API response.
    refill_dtos.sort(key=lambda r: r.scheduled_for, reverse=True)
    refill_dtos = refill_dtos[:30]

    lab_rows = await lab_followups_repo.list_for_patient(db, patient_id)
    lab_dtos = [_lab_to_dto(lab) for lab in lab_rows]

    bot_paused_at = getattr(p, "bot_paused_at", None)
    if bot_paused_at is not None and bot_paused_at.tzinfo is None:
        bot_paused_at = bot_paused_at.replace(tzinfo=timezone.utc)
    erased_at = getattr(p, "erased_at", None)
    if erased_at is not None and erased_at.tzinfo is None:
        erased_at = erased_at.replace(tzinfo=timezone.utc)
    consent_revoked_at = getattr(p, "consent_revoked_at", None)
    if (
        consent_revoked_at is not None
        and consent_revoked_at.tzinfo is None
    ):
        consent_revoked_at = consent_revoked_at.replace(
            tzinfo=timezone.utc
        )

    # Side-effect history. Keyed by patient.phone (the convention
    # the side_effect_handler uses when opening tickets) so the
    # query joins back. Limit 20 — the timeline UI shows newest
    # first; deeper history is one click away via the ops queue.
    side_effect_rows: list[OpsTicket] = []
    if p.phone:
        side_effect_rows = (
            await ops_tickets_repo.list_for_patient_by_category(
                db, p.phone, "side_effect_report", limit=20
            )
        )
    side_effect_dtos = [
        SideEffectReportDTO(
            ticket_id=str(t.id),
            status=t.status.value,
            priority=t.priority,
            created_at=t.created_at,
            acknowledged_at=t.acknowledged_at,
            resolved_at=t.resolved_at,
            sla_breached_at=t.sla_breached_at,
            reported_text=_extract_reported_text(t.notes),
        )
        for t in side_effect_rows
    ]

    return PatientDetailDTO(
        id=p.id,
        full_name=p.full_name,
        phone=p.phone,
        consent_sms=p.consent_sms,
        consent_voice=p.consent_voice,
        consent_email=p.consent_email,
        cohort_diabetes=p.cohort_diabetes,
        cohort_cardiac=p.cohort_cardiac,
        cohort_fall_risk=p.cohort_fall_risk,
        onboarding_step=p.onboarding_step,
        preferred_language=p.preferred_language or "en",
        bot_paused_at=bot_paused_at,
        bot_paused_reason=getattr(p, "bot_paused_reason", None),
        bot_paused_by=getattr(p, "bot_paused_by", None),
        erased_at=erased_at,
        consent_revoked_at=consent_revoked_at,
        consent_revoked_reason=getattr(p, "consent_revoked_reason", None),
        created_at=p.created_at,
        updated_at=p.updated_at,
        regimens=[_regimen_to_dto(r) for r in regimens],
        upcoming_appointments=appt_dtos,
        recent_adherence_events=adh_dtos,
        recent_refill_events=refill_dtos,
        lab_followups=lab_dtos,
        adherence_summary=summary,
        recent_side_effect_reports=side_effect_dtos,
    )


# ---- Patient timeline endpoint ----------------------------------------------


class TimelineEventDTO(BaseModel):
    """One row of the unified per-patient timeline. The kind
    determines how the UI renders the badge / icon; ``detail``
    carries discriminator-specific extras (entity ids,
    click-through links, badges) so the UI can render rich
    payloads without requiring a wider DTO every time we add a
    field."""

    occurred_at: datetime
    kind: str
    title: str
    body: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class PatientTimelineResponse(BaseModel):
    patient_id: int
    since: datetime
    until: datetime
    events: list[TimelineEventDTO]


@router.get(
    "/patients/{patient_id}/timeline",
    response_model=PatientTimelineResponse,
)
async def get_patient_timeline(
    patient_id: int,
    since: datetime | None = Query(
        default=None,
        description=(
            "Lower bound on event time (inclusive). Defaults to "
            "30 days ago when omitted."
        ),
    ),
    until: datetime | None = Query(
        default=None,
        description=(
            "Upper bound on event time (inclusive). Defaults to "
            "now when omitted."
        ),
    ),
    kinds: str | None = Query(
        default=None,
        description=(
            "Comma-separated list of timeline kinds to include. "
            "Defaults to every kind."
        ),
    ),
    limit: int = Query(default=200, ge=1, le=500),
    db: AsyncSession = Depends(get_session),
) -> PatientTimelineResponse:
    """Aggregate every signal we record for this patient into a
    single chronological stream. See
    ``services.orchestrator.patient_timeline`` for the source
    list and event-kind taxonomy."""
    from services.orchestrator import patient_timeline

    until_dt = until or datetime.now(timezone.utc)
    since_dt = since or (
        until_dt - timedelta(days=30)
    )
    if since_dt > until_dt:
        raise HTTPException(
            status_code=400,
            detail="since must be <= until",
        )

    kinds_tuple: tuple[str, ...] | None = None
    if kinds:
        raw = [k.strip() for k in kinds.split(",") if k.strip()]
        unknown = [
            k for k in raw if k not in patient_timeline.ALL_KINDS
        ]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"unknown timeline kinds: {unknown}. "
                    f"valid: {list(patient_timeline.ALL_KINDS)}"
                ),
            )
        kinds_tuple = tuple(raw)  # type: ignore[assignment]

    try:
        events = await patient_timeline.build_timeline(
            db,
            patient_db_id=patient_id,
            since=since_dt,
            until=until_dt,
            kinds=kinds_tuple,  # type: ignore[arg-type]
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    return PatientTimelineResponse(
        patient_id=patient_id,
        since=since_dt,
        until=until_dt,
        events=[
            TimelineEventDTO(
                occurred_at=e.occurred_at,
                kind=e.kind,
                title=e.title,
                body=e.body,
                detail=e.detail,
            )
            for e in events
        ],
    )


# ---- Patient language + onboarding reset -----------------------------------


class PatientLanguageUpdateRequest(BaseModel):
    preferred_language: str = Field(min_length=2, max_length=8)


@router.get("/i18n/languages", response_model=list[dict])
async def list_supported_languages() -> list[dict]:
    """Static allowlist used by the patient-detail language picker.
    Mirrors ``app/i18n.py::SUPPORTED_LANGUAGES`` so adding a language
    is a constants-only change visible in the UI immediately."""
    from app import i18n

    return [
        {"code": opt.code, "label": opt.label}
        for opt in i18n.SUPPORTED_LANGUAGES
    ]


@router.put(
    "/patients/{patient_id}/preferred-language",
    response_model=PatientDetailDTO,
)
async def update_patient_language(
    patient_id: int,
    payload: PatientLanguageUpdateRequest,
    db: AsyncSession = Depends(get_session),
) -> PatientDetailDTO:
    """Set the patient's preferred language. Validated against the
    SUPPORTED_LANGUAGES allowlist so we never store a code the LLM
    can't reasonably honour."""
    from app import i18n

    if not i18n.is_supported(payload.preferred_language):
        raise HTTPException(
            status_code=400,
            detail=(
                f"unsupported language code {payload.preferred_language!r}; "
                f"allowed: {', '.join(sorted(i18n.SUPPORTED_LANGUAGE_CODES))}"
            ),
        )
    row = await patients_repo.update_preferred_language(
        db, patient_id, preferred_language=payload.preferred_language
    )
    if row is None:
        raise HTTPException(status_code=404, detail="patient not found")
    await db.commit()
    # Re-fetch via the existing detail endpoint so we don't duplicate
    # the regimen + adherence + recap aggregation logic.
    return await get_patient_detail(patient_id, db)


@router.post("/patients/{patient_id}/reset-onboarding", response_model=PatientDetailDTO)
async def reset_patient_onboarding(
    patient_id: int,
    db: AsyncSession = Depends(get_session),
) -> PatientDetailDTO:
    """Reset a patient back to the start of the onboarding state machine.

    Useful for demos / regression testing. The next inbound from this
    patient will be intercepted by the onboarding handler and walked
    through name → cohorts → consent → done again."""
    row = await patients_repo.update_onboarding(
        db, patient_id, step="pending"
    )
    if row is None:
        raise HTTPException(status_code=404, detail="patient not found")
    await db.commit()
    # Re-fetch the full detail rather than duplicate the assembly logic.
    return await get_patient_detail(patient_id, db)


# ---- Bot pause / unpause (ops admin override) ------------------------------


class BotPauseRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=255)


@router.post(
    "/patients/{patient_id}/pause-bot",
    response_model=PatientDetailDTO,
)
async def pause_bot_endpoint(
    patient_id: int,
    payload: BotPauseRequest,
    db: AsyncSession = Depends(get_session),
) -> PatientDetailDTO:
    """Mute proactive bot outbound for this patient until ops
    explicitly unpauses. Distinct from opt-out: NO patient-facing
    ack is sent, ``consent_sms`` is unchanged. Used as an emergency
    brake when ops needs to investigate before the bot fires again
    (complaint received, LLM said something concerning, etc).

    Idempotent — re-pausing an already-paused patient updates the
    reason but preserves the original ``bot_paused_at`` timestamp
    so the audit trail captures when the pause actually started."""
    row = await patients_repo.pause_bot(
        db, patient_id, actor=payload.actor, reason=payload.reason
    )
    if row is None:
        raise HTTPException(status_code=404, detail="patient not found")
    try:
        from app.db.repositories import operator_actions as ops_audit

        await ops_audit.record(
            db,
            operator_id=payload.actor,
            action=ops_audit.ACTION_PATIENT_PAUSE,
            target_type="patient",
            target_id=patient_id,
            details={"reason": payload.reason},
        )
    except Exception:  # noqa: BLE001 — never block a successful action
        log.exception("operator audit failed for patient_pause %s", patient_id)
    await db.commit()
    return await get_patient_detail(patient_id, db)


@router.post(
    "/patients/{patient_id}/unpause-bot",
    response_model=PatientDetailDTO,
)
async def unpause_bot_endpoint(
    patient_id: int,
    x_ops_actor: str | None = Header(default=None),
    db: AsyncSession = Depends(get_session),
) -> PatientDetailDTO:
    """Clear the ops-initiated bot pause. Outbound resumes on the
    next dispatcher tick. Patient is not notified — pause / unpause
    are invisible by design."""
    row = await patients_repo.unpause_bot(db, patient_id)
    if row is None:
        raise HTTPException(status_code=404, detail="patient not found")
    try:
        from app.db.repositories import operator_actions as ops_audit

        await ops_audit.record(
            db,
            operator_id=(x_ops_actor or "ops").strip()[:128],
            action=ops_audit.ACTION_PATIENT_UNPAUSE,
            target_type="patient",
            target_id=patient_id,
            details={},
        )
    except Exception:  # noqa: BLE001
        log.exception("operator audit failed for patient_unpause %s", patient_id)
    await db.commit()
    return await get_patient_detail(patient_id, db)


class PatientErasureRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=255)
    confirm: bool = False


@router.post("/patients/{patient_id}/erase")
async def erase_patient_endpoint(
    patient_id: int,
    payload: PatientErasureRequest,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Right-of-erasure endpoint. Anonymizes the patient row +
    all PII-bearing related records IN PLACE. The operation is
    irreversible — the original PII is overwritten, not soft-
    deleted.

    Defense-in-depth: ``confirm=true`` is required in the body.
    A misclick on the UI button shouldn't be enough; the
    confirmation modal must explicitly set ``confirm=true``.
    Without this gate, an automated script accidentally hitting
    the endpoint would silently destroy patient data.

    Idempotent: re-erasing an already-erased patient is a no-op
    (returns the existing erased_at unchanged).
    """
    from services.orchestrator import patient_erasure

    if not payload.confirm:
        raise HTTPException(
            status_code=400,
            detail=(
                "erasure requires explicit confirmation; "
                "send confirm=true in the request body"
            ),
        )

    patient = await patient_erasure.erase_patient_data(
        db,
        patient_id=patient_id,
        actor=payload.actor,
        reason=payload.reason,
    )
    if patient is None:
        raise HTTPException(status_code=404, detail="patient not found")
    try:
        from app.db.repositories import operator_actions as ops_audit

        await ops_audit.record(
            db,
            operator_id=payload.actor,
            action=ops_audit.ACTION_PATIENT_ERASURE,
            target_type="patient",
            target_id=patient_id,
            details={
                "reason": payload.reason,
                "anonymized_phone": patient.phone,
            },
        )
    except Exception:  # noqa: BLE001 — never block an erasure on audit
        log.exception("operator audit failed for patient_erasure %s", patient_id)
    await db.commit()

    return {
        "patient_id": patient.id,
        "erased_at": patient.erased_at.isoformat()
        if patient.erased_at
        else None,
        "anonymized_phone": patient.phone,
    }


@router.get("/patients/{patient_id}/export")
async def export_patient_data(
    patient_id: int,
    window_days: int = 365,
    actor: str = "ops",
    x_ops_actor: str | None = Header(default=None),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """DSAR right-of-access endpoint. Returns a single JSON document
    containing the patient's data — demographics, regimens,
    appointments, adherence, labs, recaps, cohort tags, exemptions,
    side-effect reports.

    Every successful export writes an ``AuditRecord`` of type
    ``patient_data_export`` with the actor + the patient_id so we
    can answer "who exported this patient's data and when" if a
    regulator ever asks.

    Operator attribution: prefer the ``X-Ops-Actor`` request header (set by
    the ops console from the operator's session) over the legacy ``actor``
    query param — a PHI export's operator identity should not sit in the URL,
    where it leaks into access logs / proxies / browser history. The query
    param is kept as a backward-compatible fallback.

    NOTE: under the current shared-API-key model the actor is caller-asserted
    (there is no per-operator identity to bind it to); strong attribution
    requires an authentication layer. Tracked as a follow-up.

    Parameters:
        window_days: how far back time-bounded sections (adherence,
            recaps, appointments) reach. Defaults to 365 — most
            retention policies cover at least a year, and capping
            keeps the document size usable for typical patients.
    """
    from services.orchestrator import patient_export

    if window_days <= 0 or window_days > 3650:
        raise HTTPException(
            status_code=400,
            detail="window_days must be between 1 and 3650",
        )
    resolved_actor = (x_ops_actor or actor or "").strip()
    if not resolved_actor or len(resolved_actor) > 128:
        raise HTTPException(
            status_code=400, detail="actor required (max 128 chars)"
        )

    document = await patient_export.build_patient_export(
        db,
        patient_id=patient_id,
        actor=resolved_actor,
        window_days=window_days,
    )
    if document is None:
        raise HTTPException(status_code=404, detail="patient not found")
    try:
        from app.db.repositories import operator_actions as ops_audit

        await ops_audit.record(
            db,
            operator_id=resolved_actor,
            action=ops_audit.ACTION_PATIENT_EXPORT,
            target_type="patient",
            target_id=patient_id,
            details={
                "window_days": window_days,
                "header_actor": bool(x_ops_actor),
            },
        )
    except Exception:  # noqa: BLE001
        log.exception("operator audit failed for patient_export %s", patient_id)
    await db.commit()
    return document


# Resolve PatientDetailDTO's forward-reference to RegimenDTO (imported from
# routers._dtos). Without this Pydantic v2 raises on first instantiation.
PatientDetailDTO.model_rebuild()

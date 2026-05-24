"""Appointment recap (post-visit summary) endpoints. Extracted from main.py.

Doctor authors a structured recap → preview (LLM render) → send (freeform in-CSW
with quick replies, or the approved template out-of-CSW), with best-effort
caregiver fan-out.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AppointmentRecap, RecapStatus
from app.gateway_auth import gateway_auth_headers
from app.db.repositories import appointment_recaps as appointment_recaps_repo
from app.db.repositories import appointments as appointments_repo
from app.db.repositories import caregivers as caregivers_repo
from app.db.repositories import doctors as doctors_repo
from app.db.repositories import patient_inbound as patient_inbound_repo
from app.db.repositories import patients as patients_repo
from app.db.session import get_session
from services.orchestrator.recap_generator import RecapContext, generate_recap

log = logging.getLogger(__name__)

router = APIRouter()


class RecapMedItem(BaseModel):
    regimen_id: int | None = None
    name: str = Field(min_length=1, max_length=255)
    instructions: str | None = None  # used for "added" / "changed"
    change: str | None = None  # used for "changed" if instructions absent


class RecapLabItem(BaseModel):
    lab_followup_id: int | None = None
    test_name: str = Field(min_length=1, max_length=255)


class RecapStructuredPayload(BaseModel):
    """Doctor-authored structured fields. All lists optional; the renderer
    skips empty sections."""

    meds_added: list[RecapMedItem] = Field(default_factory=list)
    meds_changed: list[RecapMedItem] = Field(default_factory=list)
    meds_stopped: list[RecapMedItem] = Field(default_factory=list)
    labs_ordered: list[RecapLabItem] = Field(default_factory=list)
    next_followup_in_days: int | None = Field(default=None, ge=0, le=365)
    red_flags: list[str] = Field(default_factory=list)


class RecapDraftRequest(BaseModel):
    doctor_notes: str | None = None
    structured: RecapStructuredPayload = Field(default_factory=RecapStructuredPayload)
    authored_by: str | None = Field(default=None, max_length=128)


class RecapPreviewResponse(BaseModel):
    body: str


class RecapDTO(BaseModel):
    id: int
    appointment_id: int
    patient_id: int
    doctor_id: int
    doctor_notes: str | None
    structured_payload: dict
    generated_text: str | None
    status: str
    sent_message_id: str | None
    sent_at: datetime | None
    acknowledged_at: datetime | None
    authored_by: str | None
    created_at: datetime
    updated_at: datetime


def _recap_to_dto(row: AppointmentRecap) -> RecapDTO:
    return RecapDTO(
        id=row.id,
        appointment_id=row.appointment_id,
        patient_id=row.patient_id,
        doctor_id=row.doctor_id,
        doctor_notes=row.doctor_notes,
        structured_payload=row.structured_payload or {},
        generated_text=row.generated_text,
        status=row.status.value,
        sent_message_id=row.sent_message_id,
        sent_at=row.sent_at,
        acknowledged_at=row.acknowledged_at,
        authored_by=row.authored_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _format_appointment_date_local(when_utc: datetime, tz_name: str) -> str:
    """Format the appointment date in the doctor's TZ — close enough to
    'patient's TZ' for V1 since we don't track per-patient TZ yet."""
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tz_name)
        local = when_utc.astimezone(tz)
    except Exception:
        local = when_utc
    return local.strftime("%a %d %b, %I:%M %p").replace(" 0", " ")


async def _build_recap_context(
    db: AsyncSession, recap: AppointmentRecap
) -> RecapContext:
    appointment = await appointments_repo.get(db, recap.appointment_id)
    if appointment is None:
        raise HTTPException(status_code=404, detail="appointment missing")
    doctor = await doctors_repo.get(db, recap.doctor_id)
    patient = await patients_repo.get(db, recap.patient_id)
    if doctor is None or patient is None:
        raise HTTPException(status_code=404, detail="doctor or patient missing")

    appt_when = appointment.scheduled_for
    if appt_when.tzinfo is None:
        appt_when = appt_when.replace(tzinfo=timezone.utc)

    # Patient first name from full_name if present.
    first_name: str | None = None
    if patient.full_name:
        first_name = patient.full_name.strip().split()[0] or None

    payload = recap.structured_payload or {}
    return RecapContext(
        patient_first_name=first_name,
        doctor_name=doctor.name,
        appointment_date_local=_format_appointment_date_local(
            appt_when, doctor.timezone or "UTC"
        ),
        doctor_notes=recap.doctor_notes,
        meds_added=payload.get("meds_added", []),
        meds_changed=payload.get("meds_changed", []),
        meds_stopped=payload.get("meds_stopped", []),
        labs_ordered=payload.get("labs_ordered", []),
        next_followup_in_days=payload.get("next_followup_in_days"),
        red_flags=payload.get("red_flags", []),
        preferred_language=patient.preferred_language or "en",
    )


def _structured_to_dict(payload: RecapStructuredPayload) -> dict:
    return {
        "meds_added": [m.model_dump(exclude_none=True) for m in payload.meds_added],
        "meds_changed": [m.model_dump(exclude_none=True) for m in payload.meds_changed],
        "meds_stopped": [m.model_dump(exclude_none=True) for m in payload.meds_stopped],
        "labs_ordered": [
            lab.model_dump(exclude_none=True) for lab in payload.labs_ordered
        ],
        "next_followup_in_days": payload.next_followup_in_days,
        "red_flags": list(payload.red_flags),
    }


@router.put(
    "/appointments/{appointment_id}/recap", response_model=RecapDTO
)
async def upsert_appointment_recap(
    appointment_id: int,
    payload: RecapDraftRequest,
    db: AsyncSession = Depends(get_session),
) -> RecapDTO:
    """Create or update the draft recap for an appointment. Idempotent —
    calling it twice with the same body just refreshes the draft."""
    appointment = await appointments_repo.get(db, appointment_id)
    if appointment is None:
        raise HTTPException(status_code=404, detail="appointment not found")

    existing = await appointment_recaps_repo.get_for_appointment(db, appointment_id)
    structured_dict = _structured_to_dict(payload.structured)
    if existing is None:
        row = await appointment_recaps_repo.create_draft(
            db,
            appointment_id=appointment_id,
            patient_id=appointment.patient_id,
            doctor_id=appointment.doctor_id,
            doctor_notes=payload.doctor_notes,
            structured_payload=structured_dict,
            authored_by=payload.authored_by,
        )
        await db.commit()
        return _recap_to_dto(row)

    if existing.status != RecapStatus.draft:
        raise HTTPException(
            status_code=409, detail="recap already sent — cannot edit"
        )
    updated = await appointment_recaps_repo.update_draft(
        db,
        existing.id,
        doctor_notes=payload.doctor_notes,
        structured_payload=structured_dict,
        authored_by=payload.authored_by,
    )
    await db.commit()
    if updated is None:
        raise HTTPException(status_code=404, detail="recap not found")
    return _recap_to_dto(updated)


@router.get(
    "/appointments/{appointment_id}/recap", response_model=RecapDTO | None
)
async def get_appointment_recap(
    appointment_id: int, db: AsyncSession = Depends(get_session)
) -> RecapDTO | None:
    row = await appointment_recaps_repo.get_for_appointment(db, appointment_id)
    if row is None:
        return None
    return _recap_to_dto(row)


@router.post(
    "/appointments/{appointment_id}/recap/preview",
    response_model=RecapPreviewResponse,
)
async def preview_appointment_recap(
    appointment_id: int, db: AsyncSession = Depends(get_session)
) -> RecapPreviewResponse:
    """Generate the patient-facing recap text WITHOUT sending. Persists
    ``generated_text`` on the draft so the doctor sees the same body when
    they hit Send."""
    recap = await appointment_recaps_repo.get_for_appointment(db, appointment_id)
    if recap is None:
        raise HTTPException(status_code=404, detail="recap draft not found")
    if recap.status != RecapStatus.draft:
        # Already sent — return whatever was persisted.
        return RecapPreviewResponse(body=recap.generated_text or "")

    ctx = await _build_recap_context(db, recap)
    body = await generate_recap(ctx)
    await appointment_recaps_repo.set_generated_text(
        db, recap.id, generated_text=body
    )
    await db.commit()
    return RecapPreviewResponse(body=body)


# Approved Meta template name for outside-CSW recap sends.
# Default is ``post_visit_recap_v1`` — body-only template, single text
# param (patient first name), no QUICK_REPLY buttons. Body asks the
# patient to type OK / QUESTION which both the recap_handler and the
# orchestrator's compose path understand.
#
# When ``post_visit_recap_v2`` (with QUICK_REPLY buttons) lands at
# Meta, ops flips this env var; the orchestrator's ``_v2``-suffix gate
# in ``_send_recap_via_gateway`` then auto-injects the dynamic
# ``recap-ack-{id}`` / ``recap-question-{id}`` button payloads.
_RECAP_TEMPLATE_NAME = os.getenv(
    "WHATSAPP_RECAP_TEMPLATE_NAME", "post_visit_recap_v1"
)
# Test override: set WHATSAPP_FORCE_RECAP_TEMPLATE=1 to always use the
# template path even when the patient is in-CSW (for verifying out-of-CSW
# behaviour against a real patient who's actively chatting).
_FORCE_RECAP_TEMPLATE = (
    os.getenv("WHATSAPP_FORCE_RECAP_TEMPLATE", "0") == "1"
)
_RECAP_CSW_HOURS = 24


async def _patient_in_recap_csw(
    db: AsyncSession, patient_phone: str
) -> bool:
    """True when the patient has messaged us within the WhatsApp 24h
    customer-service window. Outside that window, freeform sends with
    interactive buttons are blocked by Meta — we must use a pre-approved
    template instead."""
    last = await patient_inbound_repo.get_last_inbound(db, patient_phone)
    if last is None:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last) <= timedelta(
        hours=_RECAP_CSW_HOURS
    )


async def _send_recap_via_gateway(
    *,
    patient_phone: str,
    patient_first_name: str | None,
    body: str,
    recap_id: int,
    in_csw: bool,
) -> str | None:
    """POST to the WhatsApp gateway. Returns the wamid (sent_message_id)
    on success, None on failure.

    In-CSW: send as freeform with interactive quick-reply buttons. The
    full recap body is delivered immediately and the patient can tap
    Got it / I have a question.

    Out-of-CSW: send the approved Meta recap template with a single
    parameter (patient first name). The template body is a short prompt
    that re-opens the CSW; once the patient replies, the doctor (or a
    follow-up sweep) can resend the full recap as freeform. We persist
    the same ``generated_text`` regardless so it stays the source of
    truth for what the patient owes a response to."""
    base = os.getenv("GATEWAY_URL", "http://localhost:8001").rstrip("/")

    if in_csw:
        payload: dict[str, Any] = {
            "phone": patient_phone,
            "body": body,
            "use_template": False,
            "quick_replies": [
                {"id": f"recap-ack-{recap_id}", "title": "Got it"},
                {
                    "id": f"recap-question-{recap_id}",
                    "title": "I have a question",
                },
            ],
        }
    else:
        salutation = (patient_first_name or "there").strip()
        payload = {
            "phone": patient_phone,
            "body": body,  # logged for audit; template owns the on-wire copy
            "use_template": True,
            "template_name": _RECAP_TEMPLATE_NAME,
            "template_params": {"1_name": salutation},
        }
        # On the v2 recap template (which has QUICK_REPLY buttons),
        # inject the dynamic button-id payloads so a tap routes back
        # to the recap_handler with the recap_id intact. v1 has no
        # button components — sending buttons against it returns an
        # "invalid component" error from Meta.
        if _RECAP_TEMPLATE_NAME.endswith("_v2"):
            payload["buttons"] = [
                {
                    "id": f"recap-ack-{recap_id}",
                    "label": "Got it",
                    "action": "recap_ack",
                },
                {
                    "id": f"recap-question-{recap_id}",
                    "label": "I have a question",
                    "action": "recap_question",
                },
            ]

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.post(
                f"{base}/send", json=payload, headers=gateway_auth_headers()
            )
            response.raise_for_status()
            data = response.json()
            return data.get("wamid")
    except httpx.HTTPError as exc:
        log.warning("recap send to gateway failed: %s", exc)
        return None


async def _fan_out_recap_to_caregivers(
    db: AsyncSession,
    *,
    patient: Any,
    patient_first_name: str | None,
    body: str,
    recap_id: int,
) -> int:
    """Send a copy of the recap to every active, consent-confirmed,
    notify_on_recap caregiver. Each caregiver is its own try/except so
    one failure (e.g. expired CSW for that phone, Meta rejection) never
    blocks the others. The patient's recap_status stays ``sent``
    regardless — caregiver fan-out is a best-effort cc, not a gate.

    Returns count of successful caregiver sends (for logging)."""
    caregivers = await caregivers_repo.list_active_recap_recipients(
        db, patient.id
    )
    sent_count = 0
    for caregiver in caregivers:
        # Each caregiver opens (or stays in) their own CSW based on
        # whether they've messaged us recently. Default to template
        # path — caregivers usually haven't messaged the bot, so freeform
        # would fail at Meta.
        in_csw = await _patient_in_recap_csw(db, caregiver.phone)
        # Caregiver-flavoured opener so the message reads correctly even
        # though we reuse the patient's recap body (which says "Hi {{1}}").
        # The on-wire content for in-CSW path is freeform so we can do
        # this. Out-of-CSW we use the same template — caregiver gets the
        # same prompt as a patient would, which is acceptable for V1.
        caregiver_body = (
            f"Hi {caregiver.full_name.split()[0] if caregiver.full_name else 'there'}, "
            f"shared on behalf of {patient.full_name or 'your loved one'}:\n\n"
            f"{body}"
        )
        try:
            wamid = await _send_recap_via_gateway(
                patient_phone=caregiver.phone,
                patient_first_name=(
                    caregiver.full_name.split()[0]
                    if caregiver.full_name
                    else None
                ),
                body=caregiver_body if in_csw else body,
                recap_id=recap_id,
                in_csw=in_csw,
            )
        except Exception:  # noqa: BLE001 — best-effort fan-out
            log.exception(
                "recap caregiver fan-out failed for caregiver %s", caregiver.id
            )
            continue
        if wamid is not None:
            sent_count += 1
        else:
            log.warning(
                "recap caregiver fan-out: gateway returned no wamid for "
                "caregiver %s (patient %s)",
                caregiver.id,
                patient.id,
            )
    return sent_count


@router.post(
    "/appointments/{appointment_id}/recap/send", response_model=RecapDTO
)
async def send_appointment_recap(
    appointment_id: int, db: AsyncSession = Depends(get_session)
) -> RecapDTO:
    recap = await appointment_recaps_repo.get_for_appointment(db, appointment_id)
    if recap is None:
        raise HTTPException(status_code=404, detail="recap draft not found")
    if recap.status != RecapStatus.draft:
        raise HTTPException(status_code=409, detail="recap already sent")

    patient = await patients_repo.get(db, recap.patient_id)
    if patient is None or not patient.phone:
        raise HTTPException(status_code=404, detail="patient phone missing")

    ctx = await _build_recap_context(db, recap)
    body = recap.generated_text or await generate_recap(ctx)
    in_csw = (not _FORCE_RECAP_TEMPLATE) and await _patient_in_recap_csw(
        db, patient.phone
    )

    wamid = await _send_recap_via_gateway(
        patient_phone=patient.phone,
        patient_first_name=ctx.patient_first_name,
        body=body,
        recap_id=recap.id,
        in_csw=in_csw,
    )
    if wamid is None:
        raise HTTPException(
            status_code=502, detail="gateway send failed — recap not sent"
        )

    updated = await appointment_recaps_repo.mark_sent(
        db, recap.id, sent_message_id=wamid, generated_text=body
    )

    # Caregiver fan-out — best effort; never gates the patient send.
    try:
        cc_count = await _fan_out_recap_to_caregivers(
            db,
            patient=patient,
            patient_first_name=ctx.patient_first_name,
            body=body,
            recap_id=recap.id,
        )
        if cc_count > 0:
            log.info(
                "recap %s cc'd to %s caregiver(s) for patient %s",
                recap.id,
                cc_count,
                patient.id,
            )
    except Exception:  # noqa: BLE001 — never break the patient send
        log.exception("recap caregiver fan-out failed; patient send still OK")

    await db.commit()
    if updated is None:
        raise HTTPException(status_code=404, detail="recap vanished")
    return _recap_to_dto(updated)

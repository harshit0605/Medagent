"""Caregiver endpoints (cc on recaps + consent flow).

Extracted from main.py. Manages caregiver records + the Meta-template consent
prompt (sent via the gateway) and manual/verbal consent confirmation.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.gateway_auth import gateway_auth_headers
from app.db.repositories import audit as audit_repo
from app.db.repositories import caregivers as caregivers_repo
from app.db.repositories import patients as patients_repo
from app.db.session import get_session

log = logging.getLogger(__name__)

router = APIRouter()


class CaregiverDTO(BaseModel):
    id: int
    patient_id: int
    full_name: str
    phone: str
    relationship_to_patient: str | None
    consent_status: str
    consent_confirmed_at: datetime | None
    consent_confirmed_by: str | None
    notify_on_recap: bool
    active: bool
    created_at: datetime
    updated_at: datetime


class CaregiverCreateRequest(BaseModel):
    full_name: str = Field(min_length=1, max_length=255)
    phone: str = Field(min_length=4, max_length=32)
    relationship_to_patient: str | None = Field(default=None, max_length=64)
    notify_on_recap: bool = True


class CaregiverUpdateRequest(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=255)
    relationship_to_patient: str | None = Field(default=None, max_length=64)
    notify_on_recap: bool | None = None
    active: bool | None = None


class CaregiverConsentRequest(BaseModel):
    confirmed_by: str = Field(min_length=1, max_length=128)


def _caregiver_to_dto(row: Any) -> CaregiverDTO:
    return CaregiverDTO(
        id=row.id,
        patient_id=row.patient_id,
        full_name=row.full_name,
        phone=row.phone,
        relationship_to_patient=row.relationship_to_patient,
        consent_status=row.consent_status,
        consent_confirmed_at=row.consent_confirmed_at,
        consent_confirmed_by=row.consent_confirmed_by,
        notify_on_recap=row.notify_on_recap,
        active=row.active,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get(
    "/patients/{patient_id}/caregivers",
    response_model=list[CaregiverDTO],
)
async def list_caregivers(
    patient_id: int,
    db: AsyncSession = Depends(get_session),
    include_inactive: bool = False,
) -> list[CaregiverDTO]:
    rows = await caregivers_repo.list_for_patient(
        db, patient_id, include_inactive=include_inactive
    )
    return [_caregiver_to_dto(r) for r in rows]


@router.post(
    "/patients/{patient_id}/caregivers",
    response_model=CaregiverDTO,
)
async def create_caregiver(
    patient_id: int,
    payload: CaregiverCreateRequest,
    db: AsyncSession = Depends(get_session),
) -> CaregiverDTO:
    patient = await patients_repo.get(db, patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="patient not found")
    row = await caregivers_repo.create(
        db,
        patient_id=patient_id,
        full_name=payload.full_name,
        phone=payload.phone,
        relationship_to_patient=payload.relationship_to_patient,
        notify_on_recap=payload.notify_on_recap,
    )
    await db.commit()
    return _caregiver_to_dto(row)


@router.put(
    "/caregivers/{caregiver_id}", response_model=CaregiverDTO
)
async def update_caregiver(
    caregiver_id: int,
    payload: CaregiverUpdateRequest,
    db: AsyncSession = Depends(get_session),
) -> CaregiverDTO:
    row = await caregivers_repo.update(
        db,
        caregiver_id,
        full_name=payload.full_name,
        relationship_to_patient=payload.relationship_to_patient,
        notify_on_recap=payload.notify_on_recap,
        active=payload.active,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="caregiver not found")
    await db.commit()
    return _caregiver_to_dto(row)


@router.post(
    "/caregivers/{caregiver_id}/confirm-consent",
    response_model=CaregiverDTO,
)
async def confirm_caregiver_consent(
    caregiver_id: int,
    payload: CaregiverConsentRequest,
    db: AsyncSession = Depends(get_session),
) -> CaregiverDTO:
    """Manually confirm a verbal consent (or operator-recorded one).
    Used when a clinician was present when the caregiver agreed in
    person. Production flow will eventually drive consent through an
    inbound YES from the caregiver's phone, but that needs an approved
    Meta template — until then this endpoint is the only path."""
    row = await caregivers_repo.confirm_consent(
        db, caregiver_id, confirmed_by=payload.confirmed_by
    )
    if row is None:
        raise HTTPException(status_code=404, detail="caregiver not found")
    await db.commit()
    return _caregiver_to_dto(row)


# Approved Meta template name for the caregiver consent prompt.
# Defaults to ``caregiver_consent_v1`` (2 text params + 2 QUICK_REPLY
# buttons "Yes, I consent" / "No, decline"). Body params:
#   {{1}} = caregiver first name
#   {{2}} = patient full name
# Buttons carry dynamic id payloads ``caregiver-confirm:N`` /
# ``caregiver-decline:N`` so a tap routes back to the orchestrator's
# caregiver_handler keyed on the row id.
_CAREGIVER_CONSENT_TEMPLATE_NAME = os.getenv(
    "WHATSAPP_CAREGIVER_CONSENT_TEMPLATE_NAME", "caregiver_consent_v1"
)


class CaregiverConsentPromptResponse(BaseModel):
    status: str
    wamid: str | None
    sent_at: datetime
    caregiver_id: int
    template_name: str


async def _send_caregiver_consent_prompt_via_gateway(
    *,
    caregiver_phone: str,
    caregiver_first_name: str,
    patient_full_name: str,
    caregiver_id: int,
) -> str | None:
    """POST a caregiver_consent_v1 template send to the gateway. Returns
    the wamid on success, None on failure. The two button payloads
    carry ``caregiver-confirm:N`` / ``caregiver-decline:N`` so a tap
    routes back through the webhook → caregiver_handler pipeline."""
    base = os.getenv("GATEWAY_URL", "http://localhost:8001").rstrip("/")
    payload = {
        "phone": caregiver_phone,
        "body": (
            f"Hi {caregiver_first_name}, {patient_full_name} has added "
            "you as a care contact. Reply YES to confirm or NO to decline."
        ),  # logged for audit; template owns the on-wire copy
        "use_template": True,
        "template_name": _CAREGIVER_CONSENT_TEMPLATE_NAME,
        "template_params": {
            "1_caregiver_name": caregiver_first_name,
            "2_patient_name": patient_full_name,
        },
        "buttons": [
            {
                "id": f"caregiver-confirm:{caregiver_id}",
                "label": "Yes, I consent",
                "action": "caregiver_confirm",
            },
            {
                "id": f"caregiver-decline:{caregiver_id}",
                "label": "No, decline",
                "action": "caregiver_decline",
            },
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.post(
                f"{base}/send", json=payload, headers=gateway_auth_headers()
            )
            response.raise_for_status()
            data = response.json()
            return data.get("wamid")
    except httpx.HTTPError as exc:
        log.warning("caregiver consent prompt send failed: %s", exc)
        return None


@router.post(
    "/caregivers/{caregiver_id}/send-consent-prompt",
    response_model=CaregiverConsentPromptResponse,
)
async def send_caregiver_consent_prompt(
    caregiver_id: int, db: AsyncSession = Depends(get_session)
) -> CaregiverConsentPromptResponse:
    """Send the Meta-approved consent template to the caregiver's
    phone with two QUICK_REPLY buttons. The caregiver tap is decoded
    by the Next.js webhook (``caregiver-confirm:N`` /
    ``caregiver-decline:N`` → marker text) and routed to the
    orchestrator's caregiver_handler.

    Hard-gated to ``consent_status=pending`` AND ``active=True`` —
    sending a prompt to a caregiver who already declined / revoked
    would be confusing. The endpoint returns 409 with a clear reason
    instead of 200-then-noop."""
    cg = await caregivers_repo.get(db, caregiver_id)
    if cg is None:
        raise HTTPException(status_code=404, detail="caregiver not found")
    if not cg.active:
        raise HTTPException(
            status_code=409,
            detail="caregiver is inactive — re-activate before sending consent",
        )
    if cg.consent_status != caregivers_repo.CONSENT_PENDING:
        raise HTTPException(
            status_code=409,
            detail=(
                f"caregiver consent is already {cg.consent_status} — "
                "no consent prompt to send"
            ),
        )
    if not cg.phone:
        raise HTTPException(
            status_code=400, detail="caregiver phone missing"
        )

    patient = await patients_repo.get(db, cg.patient_id)
    patient_name = (
        patient.full_name
        if patient and patient.full_name
        else "their care team contact"
    )
    caregiver_first = (
        cg.full_name.split()[0] if cg.full_name else "there"
    )

    wamid = await _send_caregiver_consent_prompt_via_gateway(
        caregiver_phone=cg.phone,
        caregiver_first_name=caregiver_first,
        patient_full_name=patient_name,
        caregiver_id=cg.id,
    )
    if wamid is None:
        raise HTTPException(
            status_code=502,
            detail="gateway send failed — consent prompt not sent",
        )

    # Audit row for the outbound. The full template payload is in
    # message_log via the gateway; this row tags the orchestrator-side
    # decision so future analytics can count "consent prompts sent".
    await audit_repo.log_workflow_summary(
        db,
        patient_id=cg.phone,
        outbound_mode="TEMPLATE",
        flow_action="ALLOW",
        reason_codes=["caregiver_consent_prompt_sent"],
        details={
            "caregiver_id": cg.id,
            "patient_id": cg.patient_id,
            "template_name": _CAREGIVER_CONSENT_TEMPLATE_NAME,
            "wamid": wamid,
        },
    )
    await db.commit()
    return CaregiverConsentPromptResponse(
        status="sent",
        wamid=wamid,
        sent_at=datetime.now(timezone.utc),
        caregiver_id=cg.id,
        template_name=_CAREGIVER_CONSENT_TEMPLATE_NAME,
    )


@router.post(
    "/caregivers/{caregiver_id}/revoke-consent",
    response_model=CaregiverDTO,
)
async def revoke_caregiver_consent(
    caregiver_id: int,
    db: AsyncSession = Depends(get_session),
) -> CaregiverDTO:
    row = await caregivers_repo.revoke_consent(db, caregiver_id)
    if row is None:
        raise HTTPException(status_code=404, detail="caregiver not found")
    await db.commit()
    return _caregiver_to_dto(row)

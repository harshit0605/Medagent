"""Doctor-authored outbound reply endpoint (clinician → patient).

Extracted from main.py. A real human's freeform message goes out as-is via the
gateway, hard-gated to the 24h customer-service window (no approved
doctor-authored template yet), with an audit row tagging who sent it.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import audit as audit_repo
from app.db.repositories import patient_inbound as patient_inbound_repo
from app.db.repositories import patients as patients_repo
from app.db.session import get_session

log = logging.getLogger(__name__)

router = APIRouter()


class DoctorReplyRequest(BaseModel):
    body: str = Field(min_length=1, max_length=2000)
    sent_by: str = Field(min_length=1, max_length=128)
    in_reply_to_message_id: str | None = Field(default=None, max_length=128)


class DoctorReplyResponse(BaseModel):
    status: str  # "sent" / "failed"
    wamid: str | None
    sent_at: datetime
    body: str
    sent_by: str


async def _send_doctor_reply_via_gateway(
    *, patient_phone: str, body: str
) -> str | None:
    """POST a freeform doctor-authored reply to the WhatsApp gateway.
    Returns wamid on success, None on failure. Caller must have already
    verified the patient is in-CSW — out-of-CSW we'd need an approved
    doctor-authored template and we don't have one yet."""
    base = os.getenv("GATEWAY_URL", "http://localhost:8001").rstrip("/")
    payload = {
        "phone": patient_phone,
        "body": body,
        "use_template": False,
    }
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.post(f"{base}/send", json=payload)
            response.raise_for_status()
            data = response.json()
            return data.get("wamid")
    except httpx.HTTPError as exc:
        log.warning("doctor reply gateway send failed: %s", exc)
        return None


@router.post(
    "/patients/{patient_id}/reply", response_model=DoctorReplyResponse
)
async def send_doctor_reply(
    patient_id: int,
    payload: DoctorReplyRequest,
    db: AsyncSession = Depends(get_session),
) -> DoctorReplyResponse:
    """Doctor / clinician-authored freeform reply to a patient. The
    bot's auto-handlers stay out of this — a real human's message goes
    out as-is so the patient can see clinical guidance verbatim.

    Hard-gated to in-CSW: outside the 24h customer-service window we
    can't send freeform without an approved template, and we have no
    doctor-authored template yet. The endpoint 409s in that case so
    the UI can show a "wait for the patient to message first" prompt
    rather than failing silently.

    Audit: the outbound is recorded by the gateway in ``message_log``
    automatically; here we add an audit_record tagged ``doctor_reply``
    + ``sent_by`` so the inbox UI can show "Dr Smith replied" later."""
    patient = await patients_repo.get(db, patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="patient not found")
    if not patient.phone:
        raise HTTPException(
            status_code=400, detail="patient has no phone on file"
        )

    # CSW gate.
    last_inbound = await patient_inbound_repo.get_last_inbound(
        db, patient.phone
    )
    now = datetime.now(timezone.utc)
    in_csw = last_inbound is not None and (
        now
        - (
            last_inbound
            if last_inbound.tzinfo is not None
            else last_inbound.replace(tzinfo=timezone.utc)
        )
    ) <= timedelta(hours=24)
    if not in_csw:
        raise HTTPException(
            status_code=409,
            detail=(
                "patient is outside the 24h customer-service window — "
                "no doctor-authored template approved yet, so a freeform "
                "reply can't be sent. Wait for the patient to message first."
            ),
        )

    wamid = await _send_doctor_reply_via_gateway(
        patient_phone=patient.phone, body=payload.body
    )
    if wamid is None:
        raise HTTPException(
            status_code=502,
            detail="gateway send failed — reply not sent",
        )

    # Audit trail. Body trimmed so an oversized message doesn't bloat
    # the audit table (the full body is in message_log).
    await audit_repo.log_workflow_summary(
        db,
        patient_id=patient.phone,
        outbound_mode="FREEFORM",
        flow_action="ALLOW",
        reason_codes=["doctor_reply"],
        details={
            "sent_by": payload.sent_by,
            "in_reply_to_message_id": payload.in_reply_to_message_id,
            "wamid": wamid,
            "body_excerpt": payload.body[:300],
        },
    )
    await db.commit()
    return DoctorReplyResponse(
        status="sent",
        wamid=wamid,
        sent_at=now,
        body=payload.body,
        sent_by=payload.sent_by,
    )

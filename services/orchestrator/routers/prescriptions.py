"""Prescription endpoints (LLM-parsed Rx upload review + verify/reject).

Extracted from main.py. The ops console reviews the vision-parsed regimen list,
then verifies (creating Regimens + eager-materializing dose reminders) or
rejects.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Prescription
from app.db.repositories import patients as patients_repo
from app.db.repositories import prescriptions as prescriptions_repo
from app.db.repositories import regimens as regimens_repo
from app.db.session import get_session
from services.scheduler import dose_reminders

log = logging.getLogger(__name__)

router = APIRouter()


class ParsedRegimenDTO(BaseModel):
    medication_name: str
    dose: str
    times_of_day: list[str] = Field(default_factory=list)
    frequency_text: str | None = None
    duration_days: int | None = None
    notes: str | None = None


class PrescriptionDTO(BaseModel):
    id: int
    patient_id: int
    patient_full_name: str | None = None
    source_upload_url: str
    public_path: str | None  # served by Next.js for the ops console preview
    parsed_payload: dict[str, Any]
    parsed_regimens: list[ParsedRegimenDTO]  # convenience-extracted from payload
    vision_parse_failed: bool
    confidence: str | None
    illegible: bool
    summary: str | None
    status: str  # pending / verified / rejected
    verified_at: datetime | None
    verified_by: str | None
    created_at: datetime
    updated_at: datetime


def _prescription_to_dto(
    row: Prescription, *, patient_full_name: str | None = None
) -> PrescriptionDTO:
    payload = row.parsed_payload or {}
    parsed = payload.get("parsed") or {}
    regimens_raw = parsed.get("regimens") or []
    return PrescriptionDTO(
        id=row.id,
        patient_id=row.patient_id,
        patient_full_name=patient_full_name,
        source_upload_url=row.source_upload_url,
        public_path=payload.get("public_path"),
        parsed_payload=payload,
        parsed_regimens=[
            ParsedRegimenDTO(
                medication_name=r.get("medication_name", ""),
                dose=r.get("dose", ""),
                times_of_day=list(r.get("times_of_day") or []),
                frequency_text=r.get("frequency_text"),
                duration_days=r.get("duration_days"),
                notes=r.get("notes"),
            )
            for r in regimens_raw
            if r.get("medication_name")
        ],
        vision_parse_failed=bool(payload.get("vision_parse_failed")),
        confidence=parsed.get("confidence"),
        illegible=bool(parsed.get("illegible")),
        summary=parsed.get("summary"),
        status=row.human_verification_status.value,
        verified_at=row.verified_at,
        verified_by=row.verified_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("/prescriptions", response_model=list[PrescriptionDTO])
async def list_prescriptions(
    db: AsyncSession = Depends(get_session),
    status: str | None = None,
    limit: int = 100,
) -> list[PrescriptionDTO]:
    if status == "pending":
        rows = await prescriptions_repo.list_pending(db, limit=limit)
    else:
        from sqlalchemy import desc, select

        stmt = (
            select(Prescription)
            .order_by(desc(Prescription.created_at))
            .limit(limit)
        )
        rows = list((await db.execute(stmt)).scalars().all())
    # Bulk-load patient names for the list.
    patient_cache: dict[int, str] = {}
    out: list[PrescriptionDTO] = []
    for r in rows:
        if r.patient_id not in patient_cache:
            p = await patients_repo.get(db, r.patient_id)
            patient_cache[r.patient_id] = (
                p.full_name if p else f"Patient #{r.patient_id}"
            )
        out.append(
            _prescription_to_dto(
                r, patient_full_name=patient_cache[r.patient_id]
            )
        )
    return out


@router.get("/prescriptions/{prescription_id}", response_model=PrescriptionDTO)
async def get_prescription(
    prescription_id: int, db: AsyncSession = Depends(get_session)
) -> PrescriptionDTO:
    row = await prescriptions_repo.get(db, prescription_id)
    if row is None:
        raise HTTPException(status_code=404, detail="prescription not found")
    p = await patients_repo.get(db, row.patient_id)
    return _prescription_to_dto(
        row, patient_full_name=p.full_name if p else None
    )


class PrescriptionVerifyRequest(BaseModel):
    """Clinician-edited regimens to create on verification.

    The clinician sees the LLM-parsed list in the UI and can edit before
    confirming — what we receive here is the FINAL list of regimens to
    create. ``timezone`` defaults to Asia/Kolkata when not supplied per
    regimen."""

    verified_by: str = Field(min_length=1, max_length=128)
    regimens: list[ParsedRegimenDTO] = Field(default_factory=list)
    timezone: str = "Asia/Kolkata"


@router.post(
    "/prescriptions/{prescription_id}/verify",
    response_model=PrescriptionDTO,
)
async def verify_prescription(
    prescription_id: int,
    payload: PrescriptionVerifyRequest,
    db: AsyncSession = Depends(get_session),
) -> PrescriptionDTO:
    row = await prescriptions_repo.get(db, prescription_id)
    if row is None:
        raise HTTPException(status_code=404, detail="prescription not found")
    if row.human_verification_status.value != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"prescription is already {row.human_verification_status.value}",
        )

    patient = await patients_repo.get(db, row.patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="patient row missing")

    # Persist clinician edits BEFORE flipping verified — keeps the audit
    # trail tidy if regimen creation later fails.
    payload_dict = row.parsed_payload or {}
    payload_dict.setdefault("parsed", {})["regimens"] = [
        r.model_dump() for r in payload.regimens
    ]
    payload_dict["clinician_edited_at"] = datetime.now(timezone.utc).isoformat()
    await prescriptions_repo.set_parsed_payload(
        db, prescription_id, parsed_payload=payload_dict
    )

    # Create one Regimen per parsed entry. Skip entries with empty times —
    # clinician must supply a schedule for the materializer to fire.
    today = datetime.now(timezone.utc).date()
    created_regimen_ids: list[int] = []
    for parsed in payload.regimens:
        if not parsed.medication_name or not parsed.dose:
            continue
        schedule = {
            "type": "times_of_day",
            "times": parsed.times_of_day,
            "timezone": payload.timezone,
            "frequency": "daily",
        }
        ends_on: date | None = None
        if parsed.duration_days:
            ends_on = today + timedelta(days=parsed.duration_days)
        regimen = await regimens_repo.create(
            db,
            patient_id=row.patient_id,
            medication_name=parsed.medication_name,
            dose=parsed.dose,
            schedule=schedule,
            starts_on=today,
            ends_on=ends_on,
            prescription_id=prescription_id,
        )
        created_regimen_ids.append(regimen.id)
        # Fire-and-forget materialize so the patient gets reminders without
        # waiting for the periodic loop. Failure here is non-fatal — the
        # materialize loop will catch up on its next tick.
        if patient.phone and parsed.times_of_day:
            try:
                await dose_reminders.materialize_for_regimen(
                    db, regimen, patient_phone=patient.phone
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "immediate dose materialize failed for regimen=%s", regimen.id
                )

    payload_dict["created_regimen_ids"] = created_regimen_ids
    await prescriptions_repo.set_parsed_payload(
        db, prescription_id, parsed_payload=payload_dict
    )
    verified = await prescriptions_repo.mark_verified(
        db, prescription_id, verified_by=payload.verified_by
    )
    await db.commit()
    log.info(
        "prescription %s verified by %s — created regimens %s",
        prescription_id,
        payload.verified_by,
        created_regimen_ids,
    )
    assert verified is not None
    return _prescription_to_dto(
        verified, patient_full_name=patient.full_name
    )


class PrescriptionRejectRequest(BaseModel):
    rejected_by: str = Field(min_length=1, max_length=128)
    reason: str | None = Field(default=None, max_length=500)


@router.post(
    "/prescriptions/{prescription_id}/reject",
    response_model=PrescriptionDTO,
)
async def reject_prescription(
    prescription_id: int,
    payload: PrescriptionRejectRequest,
    db: AsyncSession = Depends(get_session),
) -> PrescriptionDTO:
    row = await prescriptions_repo.get(db, prescription_id)
    if row is None:
        raise HTTPException(status_code=404, detail="prescription not found")
    if row.human_verification_status.value != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"prescription is already {row.human_verification_status.value}",
        )
    payload_dict = row.parsed_payload or {}
    if payload.reason:
        payload_dict["rejection_reason"] = payload.reason
    await prescriptions_repo.set_parsed_payload(
        db, prescription_id, parsed_payload=payload_dict
    )
    rejected = await prescriptions_repo.mark_rejected(
        db, prescription_id, rejected_by=payload.rejected_by
    )
    await db.commit()
    assert rejected is not None
    p = await patients_repo.get(db, rejected.patient_id)
    return _prescription_to_dto(
        rejected, patient_full_name=p.full_name if p else None
    )

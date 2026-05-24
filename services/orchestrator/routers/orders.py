"""Orders / refill execute layer endpoints (task #12).

Extracted from main.py. Reorder creation routes through the (replaceable)
pharmacy adapter; status advance enqueues a delivery receipt on the first
transition into ``delivered``; the substitution flow records a partner proposal
and enqueues the patient-facing approval ask.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import patients as patients_repo
from app.db.repositories import regimens as regimens_repo
from app.db.repositories import scheduled_events as scheduled_events_repo
from app.db.session import get_session

router = APIRouter()


class OrderDTO(BaseModel):
    id: int
    patient_id: int
    regimen_id: int | None
    medication_name: str
    dose: str | None
    quantity: str | None
    partner: str
    status: str
    partner_order_id: str | None
    partner_deeplink: str | None
    receipt_url: str | None
    substitution_status: str | None
    substitution_medication: str | None
    substitution_note: str | None
    notes: str | None
    requested_via: str | None
    created_at: datetime
    updated_at: datetime


class OrderCreateRequest(BaseModel):
    """Create a reorder. Provide ``regimen_id`` to reorder an existing regimen
    (med/dose are snapshotted from it), or pass ``medication_name`` directly
    for an ad-hoc order."""

    regimen_id: int | None = None
    medication_name: str | None = Field(default=None, max_length=255)
    dose: str | None = Field(default=None, max_length=128)
    quantity: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=2000)


class OrderStatusRequest(BaseModel):
    status: Literal[
        "pending", "processing", "shipped", "delivered", "canceled"
    ]


class SubstitutionProposeRequest(BaseModel):
    medication: str = Field(min_length=1, max_length=255)
    note: str | None = Field(default=None, max_length=2000)


def _order_to_dto(row: Any) -> OrderDTO:
    return OrderDTO(
        id=row.id,
        patient_id=row.patient_id,
        regimen_id=row.regimen_id,
        medication_name=row.medication_name,
        dose=row.dose,
        quantity=row.quantity,
        partner=row.partner,
        status=row.status,
        partner_order_id=row.partner_order_id,
        partner_deeplink=row.partner_deeplink,
        receipt_url=row.receipt_url,
        substitution_status=row.substitution_status,
        substitution_medication=row.substitution_medication,
        substitution_note=row.substitution_note,
        notes=row.notes,
        requested_via=row.requested_via,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.post("/patients/{patient_id}/orders", response_model=OrderDTO)
async def create_order(
    patient_id: int,
    body: OrderCreateRequest,
    db: AsyncSession = Depends(get_session),
) -> OrderDTO:
    """Place a reorder through the (replaceable) pharmacy adapter and persist
    it. Dedupes against an existing in-flight order for the same regimen."""
    from app.db.repositories import orders as orders_repo
    from services.orchestrator.pharmacy import (
        OrderRequest,
        get_pharmacy_adapter,
    )

    patient = await patients_repo.get(db, patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="patient not found")

    med_name = body.medication_name
    dose = body.dose
    if body.regimen_id is not None:
        regimen = await regimens_repo.get(db, body.regimen_id)
        if regimen is None or regimen.patient_id != patient_id:
            raise HTTPException(
                status_code=404, detail="regimen not found for patient"
            )
        med_name = med_name or regimen.medication_name
        dose = dose or regimen.dose
        existing = await orders_repo.get_open_for_regimen(db, body.regimen_id)
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail=f"an order is already in flight (id={existing.id})",
            )
    if not med_name:
        raise HTTPException(
            status_code=400,
            detail="medication_name is required when regimen_id is omitted",
        )

    adapter = get_pharmacy_adapter()
    result = await adapter.place_order(
        OrderRequest(
            patient_phone=patient.phone,
            medication_name=med_name,
            dose=dose,
            quantity=body.quantity,
            regimen_id=body.regimen_id,
        )
    )
    row = await orders_repo.create(
        db,
        patient_id=patient_id,
        regimen_id=body.regimen_id,
        medication_name=med_name,
        dose=dose,
        quantity=body.quantity,
        partner=result.partner,
        partner_order_id=result.partner_order_id,
        partner_deeplink=result.deeplink,
        status=result.status,
        requested_via="api",
        notes=body.notes,
    )
    dto = _order_to_dto(row)
    await db.commit()
    return dto


@router.get("/patients/{patient_id}/orders", response_model=list[OrderDTO])
async def list_patient_orders(
    patient_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    db: AsyncSession = Depends(get_session),
) -> list[OrderDTO]:
    from app.db.repositories import orders as orders_repo

    rows = await orders_repo.list_for_patient(db, patient_id, limit=limit)
    return [_order_to_dto(r) for r in rows]


@router.post("/orders/{order_id}/status", response_model=OrderDTO)
async def update_order_status(
    order_id: int,
    body: OrderStatusRequest,
    db: AsyncSession = Depends(get_session),
) -> OrderDTO:
    """Advance an order's fulfillment status (ops console / partner webhook).
    On the transition into ``delivered`` we enqueue a patient-facing delivery
    receipt (MVP #7)."""
    from app.db.repositories import orders as orders_repo

    existing = await orders_repo.get(db, order_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="order not found")
    was_delivered = existing.status == "delivered"

    try:
        row = await orders_repo.set_status(db, order_id, status=body.status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # First transition into "delivered" → send a receipt.
    if body.status == "delivered" and not was_delivered:
        patient = await patients_repo.get(db, row.patient_id)
        if patient is not None and patient.phone:
            await scheduled_events_repo.enqueue(
                db,
                event_type="order_receipt",
                patient_id=patient.phone,
                payload={"order_id": row.id},
                scheduled_for=datetime.now(timezone.utc),
            )

    dto = _order_to_dto(row)
    await db.commit()
    return dto


@router.post(
    "/orders/{order_id}/propose-substitution", response_model=OrderDTO
)
async def propose_order_substitution(
    order_id: int,
    body: SubstitutionProposeRequest,
    db: AsyncSession = Depends(get_session),
) -> OrderDTO:
    """Partner proposes a substitution; record it and enqueue a
    ``substitution_approval_v1`` ask so the patient can approve/decline."""
    from app.db.repositories import orders as orders_repo

    row = await orders_repo.propose_substitution(
        db, order_id, medication=body.medication, note=body.note
    )
    if row is None:
        raise HTTPException(status_code=404, detail="order not found")

    # Enqueue the patient-facing approval request (dispatcher renders it as the
    # substitution_approval_v1 template / interactive buttons).
    patient = await patients_repo.get(db, row.patient_id)
    if patient is not None and patient.phone:
        await scheduled_events_repo.enqueue(
            db,
            event_type="order_substitution_request",
            patient_id=patient.phone,
            payload={
                "order_id": row.id,
                "medication_name": row.medication_name,
                "substitution_medication": row.substitution_medication,
            },
            scheduled_for=datetime.now(timezone.utc),
        )
    dto = _order_to_dto(row)
    await db.commit()
    return dto

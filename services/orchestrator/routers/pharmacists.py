"""Pharmacist registry + ticket-routing endpoints (MVP #5 — pharmacist handoff).

A pharmacist is a pharmacy-partner operator. Refill-help / order-substitution
ops tickets can be assigned to a named pharmacist via the existing ticket
``assigned_to`` field, so pharmacy work is directed rather than landing in
generic ops.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import ops_tickets as ops_tickets_repo
from app.db.repositories import pharmacists as pharmacists_repo
from app.db.session import get_session

log = logging.getLogger(__name__)

router = APIRouter()


class PharmacistDTO(BaseModel):
    id: int
    full_name: str
    phone: str | None
    email: str | None
    pharmacy_name: str | None
    active: bool
    created_at: datetime


class PharmacistCreateRequest(BaseModel):
    full_name: str = Field(min_length=1, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    email: str | None = Field(default=None, max_length=255)
    pharmacy_name: str | None = Field(default=None, max_length=255)


class PharmacistActiveRequest(BaseModel):
    active: bool


def _to_dto(row) -> PharmacistDTO:
    return PharmacistDTO(
        id=row.id,
        full_name=row.full_name,
        phone=row.phone,
        email=row.email,
        pharmacy_name=row.pharmacy_name,
        active=row.active,
        created_at=row.created_at,
    )


@router.get("/pharmacists", response_model=list[PharmacistDTO])
async def list_pharmacists(
    include_inactive: bool = False,
    db: AsyncSession = Depends(get_session),
) -> list[PharmacistDTO]:
    rows = await pharmacists_repo.list_all(db, include_inactive=include_inactive)
    return [_to_dto(r) for r in rows]


@router.post("/pharmacists", response_model=PharmacistDTO)
async def create_pharmacist(
    payload: PharmacistCreateRequest,
    db: AsyncSession = Depends(get_session),
) -> PharmacistDTO:
    row = await pharmacists_repo.create(
        db,
        full_name=payload.full_name,
        phone=payload.phone,
        email=payload.email,
        pharmacy_name=payload.pharmacy_name,
    )
    await db.commit()
    return _to_dto(row)


@router.post("/pharmacists/{pharmacist_id}/active", response_model=PharmacistDTO)
async def set_pharmacist_active(
    pharmacist_id: int,
    payload: PharmacistActiveRequest,
    db: AsyncSession = Depends(get_session),
) -> PharmacistDTO:
    row = await pharmacists_repo.set_active(
        db, pharmacist_id, active=payload.active
    )
    if row is None:
        raise HTTPException(status_code=404, detail="pharmacist not found")
    await db.commit()
    return _to_dto(row)


class AssignTicketToPharmacistRequest(BaseModel):
    pharmacist_id: int
    actor: str | None = Field(default=None, max_length=128)


@router.post("/ops/tickets/{ticket_id}/assign-pharmacist")
async def assign_ticket_to_pharmacist(
    ticket_id: str,
    payload: AssignTicketToPharmacistRequest,
    x_ops_actor: str | None = Header(default=None),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Route a refill-help / order ticket to a named pharmacist. Sets the
    ticket's ``assigned_to`` to a ``pharmacist:{id} {name}`` label so the ops
    queue shows who owns it."""
    pharmacist = await pharmacists_repo.get(db, payload.pharmacist_id)
    if pharmacist is None or not pharmacist.active:
        raise HTTPException(
            status_code=404, detail="active pharmacist not found"
        )
    try:
        tid = int(ticket_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid ticket id")

    assignee = f"pharmacist:{pharmacist.id} {pharmacist.full_name}"[:128]
    ticket = await ops_tickets_repo.assign(
        db,
        tid,
        assigned_to=assignee,
        actor=(x_ops_actor or payload.actor or "ops"),
        note=f"routed to pharmacist {pharmacist.full_name}"
        + (f" @ {pharmacist.pharmacy_name}" if pharmacist.pharmacy_name else ""),
    )
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    await db.commit()
    return {
        "ticket_id": ticket.id,
        "assigned_to": ticket.assigned_to,
        "pharmacist_id": pharmacist.id,
    }

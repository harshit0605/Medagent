"""Ops ticket CRUD + the program dashboard. Extracted from main.py.

The ops console's ticket queue: create / list / fetch a ticket, the
lifecycle transitions (ack, resolve, assign, snooze, unsnooze, reopen,
note), and ``GET /ops/dashboard`` (program metrics + queue snapshot +
delivery rollup + alert counts). ``OpsTicketDTO`` / ``ProgramDashboardDTO``
and the ``_ticket_to_dto`` / ``_parse_ticket_id`` helpers live here since
nothing outside this cluster references them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OpsTicket
from app.db.repositories import dashboard as dashboard_repo
from app.db.repositories import delivery_metrics as delivery_metrics_repo
from app.db.repositories import ops_tickets as ops_tickets_repo
from app.db.repositories import patients as patients_repo
from app.db.repositories import scheduled_events as scheduled_events_repo
from app.db.session import get_session

log = logging.getLogger(__name__)

router = APIRouter()


# ---- Ops ticket DTOs + helpers ---------------------------------------------


class OpsTicketCreateRequest(BaseModel):
    patient_id: str = Field(min_length=1)
    category: str = Field(min_length=1)
    priority: Literal["p0", "p1", "p2", "p3"] = "p2"
    sla_minutes: int = Field(default=60, ge=1)
    notes: str | None = None


class OpsTicketUpdateRequest(BaseModel):
    actor: str | None = None
    notes: str | None = None


class OpsTicketDTO(BaseModel):
    ticket_id: str
    patient_id: str
    patient_db_id: int | None = None
    patient_full_name: str | None = None
    category: str
    priority: str
    sla_minutes: int
    status: str
    created_at: datetime
    acknowledged_at: datetime | None = None
    resolved_at: datetime | None = None
    notes: str | None = None
    assigned_to: str | None = None
    snoozed_until: datetime | None = None
    sla_due_at: datetime
    is_overdue: bool
    is_snoozed: bool
    # Persistent first-cross marker stamped by the SLA breach sweep.
    # Stays set after resolution so the UI can flag historically-
    # breached tickets and analytics can count breaches per category.
    # NULL on tickets that haven't crossed yet OR that the sweep
    # hasn't observed yet.
    sla_breached_at: datetime | None = None


class ProgramDashboardDTO(BaseModel):
    adherence_rate: float
    refill_risk_rate: float
    followup_closure_rate: float


def _ticket_to_dto(
    ticket: OpsTicket,
    *,
    patient_db_id: int | None = None,
    patient_full_name: str | None = None,
) -> OpsTicketDTO:
    created = ticket.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    sla_due_at = created + timedelta(minutes=ticket.sla_minutes)
    now = datetime.now(timezone.utc)
    snoozed_until = ticket.snoozed_until
    if snoozed_until is not None and snoozed_until.tzinfo is None:
        snoozed_until = snoozed_until.replace(tzinfo=timezone.utc)
    is_snoozed = snoozed_until is not None and snoozed_until > now
    is_overdue = (
        ticket.status.value in ("open", "acknowledged")
        and not is_snoozed
        and sla_due_at <= now
    )
    sla_breached_at = ticket.sla_breached_at
    if sla_breached_at is not None and sla_breached_at.tzinfo is None:
        sla_breached_at = sla_breached_at.replace(tzinfo=timezone.utc)
    return OpsTicketDTO(
        ticket_id=str(ticket.id),
        patient_id=ticket.patient_id,
        patient_db_id=patient_db_id,
        patient_full_name=patient_full_name,
        category=ticket.category,
        priority=ticket.priority,
        sla_minutes=ticket.sla_minutes,
        status=ticket.status.value,
        created_at=ticket.created_at,
        acknowledged_at=ticket.acknowledged_at,
        resolved_at=ticket.resolved_at,
        notes=ticket.notes,
        assigned_to=ticket.assigned_to,
        snoozed_until=snoozed_until,
        sla_due_at=sla_due_at,
        is_overdue=is_overdue,
        is_snoozed=is_snoozed,
        sla_breached_at=sla_breached_at,
    )


def _parse_ticket_id(ticket_id: str) -> int:
    try:
        return int(ticket_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="ticket not found") from exc


# ---- Ops ticket + dashboard endpoints --------------------------------------


@router.post("/ops/tickets", response_model=OpsTicketDTO)
async def create_ops_ticket(
    payload: OpsTicketCreateRequest, db: AsyncSession = Depends(get_session)
) -> OpsTicketDTO:
    ticket = await ops_tickets_repo.create(
        db,
        patient_id=payload.patient_id,
        category=payload.category,
        priority=payload.priority,
        sla_minutes=payload.sla_minutes,
        notes=payload.notes,
    )
    return _ticket_to_dto(ticket)


@router.get("/ops/tickets", response_model=list[OpsTicketDTO])
async def list_ops_tickets(
    db: AsyncSession = Depends(get_session),
    status: str | None = None,
    category: str | None = None,
    assigned_to: str | None = None,
    view: str | None = None,
    limit: int = 200,
) -> list[OpsTicketDTO]:
    """List ops tickets with optional filters.

    ``view`` shortcuts:
      - ``active`` — open/acknowledged AND not currently snoozed
      - ``snoozed`` — currently snoozed (snoozed_until > now)
      - any other value → no shortcut applied

    ``status`` and ``category`` filter to specific values; ``assigned_to``
    accepts a name or the literal ``"unassigned"`` to match NULL.
    """
    only_active = view == "active"
    only_snoozed = view == "snoozed"
    normalized_status = (
        status.strip().lower() if status else None
    )
    if normalized_status is not None and normalized_status not in {
        "open",
        "acknowledged",
        "resolved",
    }:
        raise HTTPException(status_code=400, detail="invalid status filter")
    assigned_filter: str | None = None
    if assigned_to is not None:
        assigned_filter = "" if assigned_to.strip().lower() == "unassigned" else assigned_to

    tickets = await ops_tickets_repo.list_with_filters(
        db,
        status=normalized_status,
        category=category,
        assigned_to=assigned_filter,
        only_active=only_active,
        only_snoozed=only_snoozed,
        limit=limit,
    )

    # Resolve patient names by phone (one query per unique phone — small).
    phone_to_patient: dict[str, tuple[int | None, str | None]] = {}
    for t in tickets:
        if t.patient_id not in phone_to_patient:
            p = await patients_repo.get_by_phone(db, t.patient_id)
            phone_to_patient[t.patient_id] = (
                (p.id, p.full_name) if p is not None else (None, None)
            )
    return [
        _ticket_to_dto(
            t,
            patient_db_id=phone_to_patient[t.patient_id][0],
            patient_full_name=phone_to_patient[t.patient_id][1],
        )
        for t in tickets
    ]


@router.get("/ops/tickets/{ticket_id}", response_model=OpsTicketDTO)
async def get_ops_ticket(
    ticket_id: str, db: AsyncSession = Depends(get_session)
) -> OpsTicketDTO:
    ticket = await ops_tickets_repo.get(db, _parse_ticket_id(ticket_id))
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    p = await patients_repo.get_by_phone(db, ticket.patient_id)
    return _ticket_to_dto(
        ticket,
        patient_db_id=p.id if p else None,
        patient_full_name=p.full_name if p else None,
    )


class OpsTicketAssignRequest(BaseModel):
    actor: str = Field(default="ops", min_length=1, max_length=128)
    assigned_to: str | None = None  # null/empty → unassign
    notes: str | None = None


class OpsTicketSnoozeRequest(BaseModel):
    actor: str = Field(default="ops", min_length=1, max_length=128)
    minutes: int | None = Field(default=None, ge=1, le=10080)
    until: datetime | None = None
    notes: str | None = None


class OpsTicketNoteRequest(BaseModel):
    actor: str = Field(default="ops", min_length=1, max_length=128)
    note: str = Field(min_length=1, max_length=1000)


@router.post("/ops/tickets/{ticket_id}/ack", response_model=OpsTicketDTO)
async def acknowledge_ops_ticket(
    ticket_id: str,
    payload: OpsTicketUpdateRequest,
    db: AsyncSession = Depends(get_session),
) -> OpsTicketDTO:
    ticket = await ops_tickets_repo.acknowledge(
        db,
        _parse_ticket_id(ticket_id),
        at=datetime.now(timezone.utc),
        actor=payload.actor or "ops",
        notes=payload.notes,
    )
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    try:
        from app.db.repositories import operator_actions as ops_audit

        await ops_audit.record(
            db,
            operator_id=payload.actor or "ops",
            action=ops_audit.ACTION_TICKET_ACK,
            target_type="ticket",
            target_id=str(ticket.id),
            details={"notes": (payload.notes or "")[:500]} if payload.notes else {},
        )
    except Exception:
        log.exception("operator audit failed for ticket_ack %s", ticket_id)
    await db.commit()
    return _ticket_to_dto(ticket)


@router.post("/ops/tickets/{ticket_id}/resolve", response_model=OpsTicketDTO)
async def resolve_ops_ticket(
    ticket_id: str,
    payload: OpsTicketUpdateRequest,
    db: AsyncSession = Depends(get_session),
) -> OpsTicketDTO:
    ticket = await ops_tickets_repo.resolve(
        db,
        _parse_ticket_id(ticket_id),
        at=datetime.now(timezone.utc),
        actor=payload.actor or "ops",
        notes=payload.notes,
    )
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    try:
        from app.db.repositories import operator_actions as ops_audit

        await ops_audit.record(
            db,
            operator_id=payload.actor or "ops",
            action=ops_audit.ACTION_TICKET_RESOLVE,
            target_type="ticket",
            target_id=str(ticket.id),
            details={"notes": (payload.notes or "")[:500]} if payload.notes else {},
        )
    except Exception:
        log.exception("operator audit failed for ticket_resolve %s", ticket_id)
    await db.commit()
    return _ticket_to_dto(ticket)


@router.post("/ops/tickets/{ticket_id}/assign", response_model=OpsTicketDTO)
async def assign_ops_ticket(
    ticket_id: str,
    payload: OpsTicketAssignRequest,
    db: AsyncSession = Depends(get_session),
) -> OpsTicketDTO:
    ticket = await ops_tickets_repo.assign(
        db,
        _parse_ticket_id(ticket_id),
        assigned_to=payload.assigned_to,
        actor=payload.actor,
        note=payload.notes,
    )
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    await db.commit()
    return _ticket_to_dto(ticket)


@router.post("/ops/tickets/{ticket_id}/snooze", response_model=OpsTicketDTO)
async def snooze_ops_ticket(
    ticket_id: str,
    payload: OpsTicketSnoozeRequest,
    db: AsyncSession = Depends(get_session),
) -> OpsTicketDTO:
    if payload.until is None and payload.minutes is None:
        raise HTTPException(
            status_code=400,
            detail="snooze requires either `minutes` or `until`",
        )
    until = (
        payload.until
        if payload.until is not None
        else datetime.now(timezone.utc) + timedelta(minutes=payload.minutes or 60)
    )
    ticket = await ops_tickets_repo.snooze(
        db,
        _parse_ticket_id(ticket_id),
        until=until,
        actor=payload.actor,
        note=payload.notes,
    )
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    await db.commit()
    return _ticket_to_dto(ticket)


@router.post("/ops/tickets/{ticket_id}/unsnooze", response_model=OpsTicketDTO)
async def unsnooze_ops_ticket(
    ticket_id: str,
    payload: OpsTicketUpdateRequest,
    db: AsyncSession = Depends(get_session),
) -> OpsTicketDTO:
    ticket = await ops_tickets_repo.unsnooze(
        db,
        _parse_ticket_id(ticket_id),
        actor=payload.actor or "ops",
        note=payload.notes,
    )
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    await db.commit()
    return _ticket_to_dto(ticket)


@router.post("/ops/tickets/{ticket_id}/reopen", response_model=OpsTicketDTO)
async def reopen_ops_ticket(
    ticket_id: str,
    payload: OpsTicketUpdateRequest,
    db: AsyncSession = Depends(get_session),
) -> OpsTicketDTO:
    ticket = await ops_tickets_repo.reopen(
        db,
        _parse_ticket_id(ticket_id),
        actor=payload.actor or "ops",
        note=payload.notes,
    )
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    await db.commit()
    return _ticket_to_dto(ticket)


@router.post("/ops/tickets/{ticket_id}/note", response_model=OpsTicketDTO)
async def add_ops_ticket_note(
    ticket_id: str,
    payload: OpsTicketNoteRequest,
    db: AsyncSession = Depends(get_session),
) -> OpsTicketDTO:
    ticket = await ops_tickets_repo.append_note(
        db,
        _parse_ticket_id(ticket_id),
        actor=payload.actor,
        note=payload.note,
    )
    if ticket is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    await db.commit()
    return _ticket_to_dto(ticket)


# ---- /ops/dashboard — program metrics + queue + alerts ---------------------


@router.get("/ops/dashboard")
async def get_ops_dashboard(db: AsyncSession = Depends(get_session)) -> dict:
    metrics = await dashboard_repo.program_metrics(db)
    queue = await ops_tickets_repo.snapshot(db)
    # Last 24h delivery rollup. Joins message_log → whatsapp_message_statuses
    # so the dashboard shows what actually happened to the messages we
    # claim to have sent (Meta-side delivery vs failure).
    delivery = await delivery_metrics_repo.delivery_summary(db)
    # Per-template breakdown so the UI can flag a single template
    # silently failing while the aggregate stays healthy. Sorted by
    # volume descending — high-traffic templates lead.
    delivery_by_template = (
        await delivery_metrics_repo.delivery_summary_by_template(db)
    )
    # Scheduled-event dead-letter queue count. Non-zero means
    # transient failures exhausted their retry budget and ops
    # needs to investigate (Meta API change, expired template,
    # malformed payload, etc.). The dashboard tile renders this
    # like the other "alerts" — count + click-through to the
    # /dlq page.
    scheduled_events_dlq = await scheduled_events_repo.count_dlq(db)
    return {
        "program_metrics": ProgramDashboardDTO(**metrics),
        "queue": queue,
        "delivery": delivery,
        "delivery_by_template": delivery_by_template,
        "alerts": {
            "regimens_running_low": await dashboard_repo.regimens_running_low_count(db),
            "missed_dose_escalations_open": await dashboard_repo.missed_dose_escalations_open_count(db),
            "refill_help_open": await dashboard_repo.refill_help_open_count(db),
            "labs_overdue": await dashboard_repo.labs_overdue_count(db),
            "lab_help_open": await dashboard_repo.lab_help_open_count(db),
            "prescriptions_pending": await dashboard_repo.prescriptions_pending_count(db),
            "tickets_sla_overdue": await dashboard_repo.tickets_sla_overdue_count(db),
            "care_gaps_open": await dashboard_repo.care_gaps_open_count(db),
            "scheduled_events_dlq": scheduled_events_dlq,
        },
    }

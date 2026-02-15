from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from medagent import FakeGateway, InMemoryStore, MedAgentFlow

from shared.contracts.models import IntentType, MessageIn, MessageOut, QuickReply
from services.orchestrator.agent_workflow import run_agent_workflow

app = FastAPI(title="orchestrator")
store = InMemoryStore()
gateway = FakeGateway()
flow = MedAgentFlow(store=store, gateway=gateway)


class OrchestratorRequest(BaseModel):
    message: MessageIn
    last_user_message_at: datetime | None = None


class PolicyDecision(BaseModel):
    in_customer_service_window: bool
    use_template: bool
    reason: str


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
    category: str
    priority: str
    sla_minutes: int
    status: str
    created_at: datetime
    acknowledged_at: datetime | None = None
    resolved_at: datetime | None = None
    notes: str | None = None


class ProgramDashboardDTO(BaseModel):
    adherence_rate: float
    refill_risk_rate: float
    followup_closure_rate: float


def _ticket_to_dto(ticket) -> OpsTicketDTO:
    return OpsTicketDTO(
        ticket_id=ticket.ticket_id,
        patient_id=ticket.patient_id,
        category=ticket.category,
        priority=ticket.priority,
        sla_minutes=ticket.sla_minutes,
        status=ticket.status,
        created_at=ticket.created_at,
        acknowledged_at=ticket.acknowledged_at,
        resolved_at=ticket.resolved_at,
        notes=ticket.notes,
    )


def detect_intent(text: str | None) -> IntentType:
    if not text:
        return IntentType.GENERAL_QUESTION

    lower = text.lower()
    if any(x in lower for x in ["taken", "snooze", "skip", "missed", "1", "2", "3"]):
        return IntentType.ADHERENCE_UPDATE
    if any(x in lower for x in ["forgot", "side effect", "out of stock", "confused", "cost"]):
        return IntentType.ADHERENCE_UPDATE
    if any(x in lower for x in ["refill", "reorder", "run out", "update count"]):
        return IntentType.REFILL_REQUEST
    if any(x in lower for x in ["lab", "hba1c", "appointment", "follow-up", "followup"]):
        return IntentType.GENERAL_QUESTION
    if any(x in lower for x in ["symptom", "breath", "pain", "dizzy", "fever", "bleeding", "wheezing", "hypo", "high bp"]):
        return IntentType.SYMPTOM_REPORT
    if "pregnan" in lower or "trimester" in lower:
        return IntentType.PREGNANCY_CHECKLIST
    return IntentType.GENERAL_QUESTION


def policy_gate(now: datetime, last_user_message_at: datetime | None) -> PolicyDecision:
    if last_user_message_at is None:
        return PolicyDecision(
            in_customer_service_window=False,
            use_template=True,
            reason="No prior inbound message timestamp; require template send",
        )

    if last_user_message_at.tzinfo is None:
        last_user_message_at = last_user_message_at.replace(tzinfo=timezone.utc)
    else:
        last_user_message_at = last_user_message_at.astimezone(timezone.utc)

    in_window = (now - last_user_message_at) <= timedelta(hours=24)
    return PolicyDecision(
        in_customer_service_window=in_window,
        use_template=not in_window,
        reason="Within 24h freeform allowed" if in_window else "Outside 24h template required",
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/route")
def route(payload: OrchestratorRequest) -> dict:
    now = datetime.now(timezone.utc)

    result = run_agent_workflow(
        message_id=payload.message.message_id,
        patient_id=payload.message.patient_id or payload.message.message_id,
        text=payload.message.text,
        phone=payload.message.phone,
        last_user_message_at=payload.last_user_message_at,
        now=now,
    )

    intent_map = {
        "adherence_update": IntentType.ADHERENCE_UPDATE,
        "refill_request": IntentType.REFILL_REQUEST,
        "symptom_report": IntentType.SYMPTOM_REPORT,
        "pregnancy_checklist": IntentType.PREGNANCY_CHECKLIST,
        "followup_update": IntentType.GENERAL_QUESTION,
        "general_question": IntentType.GENERAL_QUESTION,
    }
    intent = intent_map[result.intent]
    decision = PolicyDecision(
        in_customer_service_window=not result.use_template,
        use_template=result.use_template,
        reason=result.policy_reason,
    )

    msg = MessageOut(
        patient_id=payload.message.patient_id or payload.message.message_id,
        phone=payload.message.phone,
        body=result.response_body,
        use_template=result.use_template,
        template_name=result.template_name,
        quick_replies=[QuickReply(id=reply.lower(), title=reply) for reply in result.quick_replies],
    )
    return {
        "intent": intent,
        "policy": decision,
        "risk_level": result.risk_level,
        "escalation_required": result.escalation_required,
        "audit_reasons": result.audit_reasons,
        "message_out": msg,
    }


@app.post("/ops/tickets", response_model=OpsTicketDTO)
def create_ops_ticket(payload: OpsTicketCreateRequest) -> OpsTicketDTO:
    ticket = flow.create_ops_ticket(
        patient_id=payload.patient_id,
        category=payload.category,
        priority=payload.priority,
        sla_minutes=payload.sla_minutes,
        created_at=datetime.now(timezone.utc),
        notes=payload.notes,
    )
    return _ticket_to_dto(ticket)


@app.get("/ops/tickets", response_model=list[OpsTicketDTO])
def list_ops_tickets(status: str | None = None) -> list[OpsTicketDTO]:
    tickets = list(store.ops_tickets.values())
    if status is not None:
        normalized_status = status.strip().lower()
        if normalized_status not in {"open", "acknowledged", "resolved"}:
            raise HTTPException(status_code=400, detail="invalid status filter")
        tickets = [ticket for ticket in tickets if ticket.status == normalized_status]
    tickets.sort(key=lambda ticket: ticket.created_at, reverse=True)
    return [_ticket_to_dto(ticket) for ticket in tickets]


@app.post("/ops/tickets/{ticket_id}/ack", response_model=OpsTicketDTO)
def acknowledge_ops_ticket(ticket_id: str, payload: OpsTicketUpdateRequest) -> OpsTicketDTO:
    try:
        ticket = flow.acknowledge_ops_ticket(ticket_id=ticket_id, at=datetime.now(timezone.utc))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="ticket not found") from exc

    if payload.notes:
        ticket.notes = payload.notes
    return _ticket_to_dto(ticket)


@app.post("/ops/tickets/{ticket_id}/resolve", response_model=OpsTicketDTO)
def resolve_ops_ticket(ticket_id: str, payload: OpsTicketUpdateRequest) -> OpsTicketDTO:
    note = payload.notes or (f"resolved by {payload.actor}" if payload.actor else None)
    try:
        ticket = flow.resolve_ops_ticket(ticket_id=ticket_id, at=datetime.now(timezone.utc), notes=note)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="ticket not found") from exc
    return _ticket_to_dto(ticket)


@app.get("/ops/dashboard")
def get_ops_dashboard() -> dict:
    dashboard = flow.build_program_dashboard()
    queue_snapshot = flow.ops_queue_snapshot()
    return {
        "program_metrics": ProgramDashboardDTO(
            adherence_rate=dashboard.adherence_rate,
            refill_risk_rate=dashboard.refill_risk_rate,
            followup_closure_rate=dashboard.followup_closure_rate,
        ),
        "queue": queue_snapshot,
    }

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    OpsTicket,
)
from app.db.repositories import audit as audit_repo
from app.db.repositories import (
    inbound_classifications as inbound_classifications_repo,
)
from app.db.repositories import dashboard as dashboard_repo
from app.db.repositories import delivery_metrics as delivery_metrics_repo
from app.db.repositories import message_log as message_log_repo
from app.db.repositories import ops_tickets as ops_tickets_repo
from app.db.repositories import patient_inbound as patient_inbound_repo
from app.db.repositories import patients as patients_repo
from app.db.repositories import scheduled_events as scheduled_events_repo
from app.db.session import get_session
from services.orchestrator.inbox_classifier import (
    Classification,
    classify_inbound,
    is_action_tap,
)
from services.orchestrator import transcription
from services.orchestrator.routers import care_plan_exemptions as care_plan_exemptions_router
from services.orchestrator.routers import care_plan_goals as care_plan_goals_router
from services.orchestrator.routers import appointment_recap as appointment_recap_router
from services.orchestrator.routers import care_plans as care_plans_router
from services.orchestrator.routers import caregivers as caregivers_router
from services.orchestrator.routers import audit_search as audit_search_router
from services.orchestrator.routers import broadcast as broadcast_router
from services.orchestrator.routers import clinical_alerts as clinical_alerts_router
from services.orchestrator.routers import cohort_tags as cohort_tags_router
from services.orchestrator.routers import dlq as dlq_router
from services.orchestrator.routers import doctor_inbox as doctor_inbox_router
from services.orchestrator.routers import doctor_replies as doctor_replies_router
from services.orchestrator.routers import doctors as doctors_router
from services.orchestrator.routers import households as households_router
from services.orchestrator.routers import lab_followups as lab_followups_router
from services.orchestrator.routers import llm_cost_analytics as llm_cost_analytics_router
from services.orchestrator.routers import ops_analytics as ops_analytics_router
from services.orchestrator.routers import ops_health as ops_health_router
from services.orchestrator.routers import orders as orders_router
from services.orchestrator.routers import post_op as post_op_router
from services.orchestrator.routers import patients as patients_router
from services.orchestrator.routers import pregnancy as pregnancy_router
from services.orchestrator.routers import prescriptions as prescriptions_router
from services.orchestrator.routers import pre_visit as pre_visit_router
from services.orchestrator.routers import regimen as regimen_router
from services.orchestrator.routers import side_effect_analytics as side_effect_analytics_router
from services.orchestrator.routers import visit_briefs as visit_briefs_router
from services.orchestrator.agent_workflow import (
    AgentState,
    build_langgraph_workflow,
    run_agent_workflow,
)
from shared.contracts.models import (
    ActionButton,
    IntentType,
    ListRow,
    MessageIn,
    MessageOut,
    QuickReply,
)

log = logging.getLogger(__name__)


def _libpq_url(database_url: str) -> str:
    """Strip the SQLAlchemy ``+psycopg`` dialect tag to get a libpq-compatible URL."""
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


async def _make_compiled_graph() -> dict[str, Any] | None:
    """Construct the AsyncConnectionPool, AsyncPostgresSaver, and compiled graph.

    Returns ``None`` and logs a warning when LangGraph is disabled or the
    pool/saver fails to come up — the orchestrator continues with the sync
    fallback in that case so a momentary DB blip doesn't 500 every request.

    The checkpointer pool prefers ``DIRECT_URL`` over ``DATABASE_URL`` because
    Supabase's pooler uses PgBouncer transaction-mode, which is incompatible
    with the prepared statements LangGraph's checkpointer relies on.
    """
    if os.getenv("LANGGRAPH_ENABLED", "1") == "0":
        log.info("LANGGRAPH_ENABLED=0 — skipping compiled-graph init")
        return None
    db_url = os.getenv("DIRECT_URL") or os.getenv("DATABASE_URL")
    if not db_url:
        log.warning("Neither DIRECT_URL nor DATABASE_URL set — compiled graph disabled")
        return None
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool
    except ImportError as exc:
        log.warning("LangGraph or psycopg-pool not installed: %s", exc)
        return None

    pool = AsyncConnectionPool(
        conninfo=_libpq_url(db_url),
        max_size=4,
        kwargs={
            "autocommit": True,
            # ``None`` fully disables psycopg's auto-prepared-statement cache.
            "prepare_threshold": None,
            "row_factory": dict_row,
        },
        # Validate each connection before handing it out — Supabase kills
        # idle connections silently, and without this the checkpointer
        # blows up on the next turn with "server closed the connection
        # unexpectedly". One-trip overhead per checkout (~ms); worth it.
        check=AsyncConnectionPool.check_connection,
        # Cap idle time below Supabase's typical idle-timeout window.
        max_idle=300.0,
        open=False,
    )
    await pool.open(wait=True, timeout=10)
    try:
        checkpointer = AsyncPostgresSaver(pool)
        await checkpointer.setup()
        graph = build_langgraph_workflow(checkpointer=checkpointer)
        if graph is None:
            log.warning("build_langgraph_workflow returned None")
            await pool.close()
            return None
    except Exception as exc:  # noqa: BLE001 — degrade gracefully
        log.warning("AsyncPostgresSaver setup failed: %s — falling back to sync runner", exc)
        await pool.close()
        return None

    return {"graph": graph, "pool": pool}


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not os.getenv("ORCHESTRATOR_API_KEY", ""):
        log.warning(
            "ORCHESTRATOR_API_KEY is unset — the orchestrator HTTP API is "
            "UNAUTHENTICATED. Set it (and the ops console's matching env) "
            "before exposing this service."
        )
    state = await _make_compiled_graph()
    if state is not None:
        app.state.graph = state["graph"]
        app.state.checkpoint_pool = state["pool"]
    else:
        app.state.graph = None
        app.state.checkpoint_pool = None
    try:
        yield
    finally:
        pool = getattr(app.state, "checkpoint_pool", None)
        if pool is not None:
            await pool.close()


app = FastAPI(title="orchestrator", lifespan=lifespan)


# Shared-secret API key for the orchestrator's HTTP surface. When
# ``ORCHESTRATOR_API_KEY`` is set, every request must present a matching
# ``X-API-Key`` (or ``Authorization: Bearer``) header — the Next.js ops
# console / ingress send it via their backend client. When UNSET, auth is
# disabled (a loud startup warning fires) so local dev + tests keep working.
# Health checks and the Google-Calendar webhook (which carries its own
# channel-token secret) are exempt.
_ORCH_API_KEY = os.getenv("ORCHESTRATOR_API_KEY", "")
_AUTH_EXEMPT_PREFIXES: tuple[str, ...] = (
    "/health",
    "/webhooks/",
    "/docs",
    "/redoc",
    "/openapi.json",
)


def _auth_exempt(path: str) -> bool:
    return any(
        path == p or path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES
    )


def _extract_api_key(request: Request) -> str | None:
    key = request.headers.get("x-api-key")
    if key:
        return key
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


@app.middleware("http")
async def _api_key_auth(request: Request, call_next):
    if _ORCH_API_KEY and not _auth_exempt(request.url.path):
        provided = _extract_api_key(request)
        if not (provided and hmac.compare_digest(provided, _ORCH_API_KEY)):
            return JSONResponse(
                status_code=401,
                content={"detail": "invalid or missing API key"},
            )
    return await call_next(request)


# Domain routers extracted from this module (incremental decomposition of the
# historically-monolithic main.py). Each is self-contained; see
# services/orchestrator/routers/.
app.include_router(orders_router.router)
app.include_router(pregnancy_router.router)
app.include_router(post_op_router.router)
app.include_router(households_router.router)
app.include_router(visit_briefs_router.router)
app.include_router(care_plans_router.router)
app.include_router(cohort_tags_router.router)
app.include_router(care_plan_exemptions_router.router)
app.include_router(caregivers_router.router)
app.include_router(clinical_alerts_router.router)
app.include_router(care_plan_goals_router.router)
app.include_router(lab_followups_router.router)
app.include_router(regimen_router.router)
app.include_router(prescriptions_router.router)
app.include_router(llm_cost_analytics_router.router)
app.include_router(side_effect_analytics_router.router)
app.include_router(audit_search_router.router)
app.include_router(dlq_router.router)
app.include_router(doctor_inbox_router.router)
app.include_router(broadcast_router.router)
app.include_router(doctor_replies_router.router)
app.include_router(ops_health_router.router)
app.include_router(ops_analytics_router.router)
app.include_router(appointment_recap_router.router)
app.include_router(doctors_router.router)
app.include_router(pre_visit_router.router)
app.include_router(patients_router.router)


def _get_graph(request: Request) -> Any | None:
    return getattr(request.app.state, "graph", None)


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


_INTENT_MAP = {
    "adherence_update": IntentType.ADHERENCE_UPDATE,
    "refill_request": IntentType.REFILL_REQUEST,
    "symptom_report": IntentType.SYMPTOM_REPORT,
    "pregnancy_checklist": IntentType.PREGNANCY_CHECKLIST,
    "followup_update": IntentType.GENERAL_QUESTION,
    # No dedicated IntentType for booking yet — surface it as a general question
    # to existing API consumers; the runner field tells you it actually went
    # through booking_agent.
    "booking_request": IntentType.GENERAL_QUESTION,
    "general_question": IntentType.GENERAL_QUESTION,
}


async def _invoke_graph(
    graph: Any,
    *,
    message: MessageIn,
    patient_id: str,
    last_seen: datetime | None,
    now: datetime,
) -> dict[str, Any]:
    initial_state: AgentState = {
        "message_id": message.message_id,
        "patient_id": patient_id,
        "phone": message.phone,
        "text": (message.text or "").strip(),
        "now_utc": now,
        "last_user_message_at": last_seen,
    }
    # Per-PATIENT thread (was per-message). Lets the checkpointer accumulate
    # the conversation history across turns so multi-turn flows (e.g. booking)
    # can resume from prior state. The patient_id here is the WhatsApp wa_id.
    config = {"configurable": {"thread_id": f"patient:{patient_id}"}}
    return await graph.ainvoke(initial_state, config=config)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def _infer_handler_used(audit_reasons: list[str]) -> str:
    """Map a workflow's audit reason-code list back to the handler that
    actually processed the inbound. Used by the doctor-inbox row so a
    clinician can see which path served the patient (or whether ops
    had to step in)."""
    reasons = " ".join(audit_reasons).lower()
    # Order matters: prescription_handler emits the prescription marker
    # ALONGSIDE generic compose codes, so check it before the generic
    # fallback. Same for the others.
    if "dose_action" in reasons:
        return "dose_handler"
    if "refill_action" in reasons:
        return "refill_handler"
    if "lab_action" in reasons:
        return "lab_handler"
    if "recap_action" in reasons:
        return "recap_handler"
    if "prescription" in reasons:
        return "prescription_handler"
    if "onboarding" in reasons:
        return "onboarding_handler"
    if "booking_agent" in reasons:
        return "booking_agent"
    if "human_escalation_exposed" in reasons or "human_handoff" in reasons:
        return "human_handoff"
    return "llm_compose"


async def _persist_clinical_alert_if_urgent(
    db: AsyncSession,
    *,
    inbound_text: str | None,
    intent_str: str,
    patient_phone: str,
    message_log_id: int | None,
    inbound_classification_id: int | None = None,
) -> None:
    """Run the triage classifier and persist a ``clinical_alerts``
    row when severity is ``high`` or ``critical``. Best-effort —
    a triage failure must not break /route, since the bot's
    primary reply has already been sent.

    The row's ``message_id`` FKs to ``message_log.id`` (the
    inbound row we just inserted) so the alerts UI can link to
    the verbatim original. ``inbound_text`` is also snapshotted
    on the alert row in case the message log is later trimmed
    by retention.
    """
    if not inbound_text or not inbound_text.strip():
        return
    try:
        from services.orchestrator import triage_classifier

        decision = await triage_classifier.classify_clinical_urgency(
            text=inbound_text, intent=intent_str
        )
        if decision.severity not in triage_classifier.ALERT_SEVERITIES:
            return
        # Resolve patient_id from phone — alerts FK to patients.
        patient_db_id: int | None = None
        try:
            patient_row = await patients_repo.get_by_phone(
                db, patient_phone
            )
            if patient_row is not None:
                patient_db_id = patient_row.id
        except Exception:  # noqa: BLE001 — non-fatal lookup
            patient_db_id = None
        if patient_db_id is None:
            log.warning(
                "clinical alert skipped: phone %s not in patients",
                patient_phone,
            )
            return
        from app.db.repositories import (
            clinical_alerts as clinical_alerts_repo,
        )
        from services.orchestrator.llm import get_llm

        llm = get_llm()
        alert_row = await clinical_alerts_repo.create(
            db,
            patient_id=patient_db_id,
            patient_phone=patient_phone,
            message_id=message_log_id,
            severity=decision.severity,
            red_flags=list(decision.red_flags or []),
            clinical_summary=decision.summary,
            inbound_text=inbound_text,
            llm_model=getattr(llm, "model", None),
        )
        log.info(
            "clinical alert created (severity=%s, patient=%s, "
            "red_flags=%s)",
            decision.severity,
            patient_phone,
            decision.red_flags,
        )
        # Denormalise severity onto the inbound_classification
        # row so the inbox UI can render a small badge without
        # fetching alerts. Tied via the classification id we
        # received from the inbox-persistence step — message_id
        # joins are awkward (wamid is in JSON for inbound rows).
        if inbound_classification_id is not None:
            try:
                from app.db.models import InboundClassification

                ic_row = await db.get(
                    InboundClassification,
                    inbound_classification_id,
                )
                if ic_row is not None:
                    ic_row.clinical_severity = decision.severity
                    await db.flush()
            except Exception:  # noqa: BLE001 — non-fatal
                log.exception(
                    "inbound_classification clinical_severity "
                    "stamp failed; alert exists but inbox "
                    "row won't show the badge"
                )
        # Critical severity → enqueue an immediate paging
        # event. ``high`` lands in the queue but doesn't
        # actively page, by design. The dispatcher's
        # ``clinical_alert_page`` branch picks it up on the
        # next tick (within the scheduler poll interval — a
        # few seconds in production).
        if decision.severity == "critical":
            try:
                from app.db.repositories import (
                    scheduled_events as scheduled_events_repo,
                )

                await scheduled_events_repo.enqueue(
                    db,
                    event_type="clinical_alert_page",
                    patient_id=patient_phone,
                    payload={
                        "alert_id": alert_row.id,
                        "patient_db_id": patient_db_id,
                        "severity": decision.severity,
                    },
                    scheduled_for=datetime.now(timezone.utc),
                )
            except Exception:  # noqa: BLE001 — non-fatal
                log.exception(
                    "clinical alert paging enqueue failed; "
                    "alert %s exists but won't auto-page",
                    alert_row.id,
                )
    except Exception:  # noqa: BLE001 — never fatal
        log.exception(
            "clinical alert persistence failed; ignoring"
        )


async def _persist_inbox_classification(
    db: AsyncSession,
    *,
    message_id: str | None,
    patient_phone: str,
    inbound_text: str | None,
    intent_str: str,
    audit_reasons: list[str],
    response_body: str,
    escalation_required: bool,
    ticket_id: str | None,
    input_kind: str | None = None,
    request_duration_ms: int | None = None,
) -> int | None:
    """Best-effort insert into ``inbound_classifications``. Wrapped in a
    try/except so a classifier or DB failure never breaks the /route
    response — the inbox view degrades to "no row" rather than dropping
    the whole request.

    Returns the created row's id on success so the clinical-
    alert hook can denormalise severity onto it. Returns
    ``None`` on any failure path — caller skips the
    denormalisation step in that case.
    """
    try:
        classification: Classification = await classify_inbound(
            text=inbound_text, intent=intent_str
        )
        # Resolve the patient's DB id by phone so the inbox row links
        # to /patients/{id} cleanly. None if we can't find them.
        patient_db_id: int | None = None
        try:
            patient_row = await patients_repo.get_by_phone(db, patient_phone)
            if patient_row is not None:
                patient_db_id = patient_row.id
        except Exception:  # noqa: BLE001 — non-fatal lookup
            patient_db_id = None
        ticket_int: int | None = None
        if ticket_id is not None:
            try:
                ticket_int = int(ticket_id)
            except (TypeError, ValueError):
                ticket_int = None
        ic_row = await inbound_classifications_repo.create(
            db,
            message_id=message_id,
            patient_phone=patient_phone,
            patient_db_id=patient_db_id,
            inbound_text=inbound_text,
            category=classification.category,
            summary=classification.summary,
            urgency=classification.urgency,
            handler_used=_infer_handler_used(audit_reasons),
            response_text=response_body,
            escalated=escalation_required or ticket_int is not None,
            ticket_id=ticket_int,
            input_kind=input_kind or "text",
            request_duration_ms=request_duration_ms,
        )
        return ic_row.id
    except Exception:  # noqa: BLE001 — read-side annotation, never fatal
        log.exception("inbox classification persistence failed; ignoring")
        return None


async def _handle_rate_limited(
    db: AsyncSession,
    *,
    payload: "OrchestratorRequest",
    patient_id: str,
    decision: Any,
    now: datetime,
) -> dict:
    """Handle the early-return path when the inbound rate-limit gate
    fires. Logs the inbound to ``message_log`` for forensics, writes
    an audit row, opens an ops ticket once per patient per UTC day
    (idempotent — a sustained burst doesn't open a ticket per
    minute), and returns a minimal MessageOut with empty body so the
    gateway sends nothing.

    The audit + ticket writes are wrapped — the rate-limit gate
    must NEVER raise into the route handler. If something goes
    wrong with the logging/ticketing, we still want the early-
    return to succeed and the inbound to be silently dropped.
    """
    try:
        await message_log_repo.append_inbound(
            db,
            patient_id=patient_id,
            payload=payload.message.model_dump(mode="json"),
            occurred_at=now,
        )
    except Exception:  # noqa: BLE001 — best-effort, never block the early-return
        log.exception(
            "rate-limited inbound log failed; continuing the early-return"
        )

    try:
        await audit_repo.log_workflow_summary(
            db,
            patient_id=patient_id,
            outbound_mode=None,
            flow_action="HOLD",
            reason_codes=["rate_limited"],
            details={
                "count": decision.count,
                "limit": decision.limit,
                "window_minutes": decision.window_minutes,
                "message_id": payload.message.message_id,
            },
            logged_at=now,
        )
    except Exception:  # noqa: BLE001
        log.exception("rate-limited audit log failed; continuing")

    # Idempotent ticket open — one ticket per patient per UTC day,
    # regardless of how many subsequent rate-limit hits land in
    # that day. The ticket points to the underlying behaviour
    # (loop, abuse, retry storm) for ops to investigate.
    try:
        existing = await ops_tickets_repo.find_open_for_patient_category(
            db,
            patient_id=patient_id,
            category="inbound_rate_limit",
        )
        if existing is None:
            notes = (
                f"Inbound rate limit exceeded: {decision.count} "
                f"messages in last {decision.window_minutes} min "
                f"(threshold {decision.limit}). Likely client retry "
                f"loop or abuse — investigate before unmuting."
            )
            await ops_tickets_repo.create(
                db,
                patient_id=patient_id,
                category="inbound_rate_limit",
                priority="high",
                sla_minutes=60,
                notes=notes,
            )
    except Exception:  # noqa: BLE001
        log.exception("rate-limited ticket open failed; continuing")

    await db.commit()

    msg = MessageOut(
        patient_id=patient_id,
        phone=payload.message.phone,
        body="",
        use_template=False,
        template_name=None,
        quick_replies=[],
        buttons=[],
        list_rows=[],
        list_button_label=None,
        list_section_title=None,
    )
    return {
        "message_out": msg.model_dump(mode="json"),
        "intent": "general_question",
        "risk_level": "low",
        "use_template": False,
        "policy_reason": "rate_limited",
        "policy_reason_codes": ["rate_limited"],
        "flow_action": "HOLD",
        "escalation_required": False,
        "audit_reasons": ["rate_limited"],
        "rate_limited": True,
        "rate_limit_count": decision.count,
        "rate_limit_window_minutes": decision.window_minutes,
    }


@app.post("/route")
async def route(
    payload: OrchestratorRequest,
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> dict:
    from services.orchestrator.rate_limiter import (
        check_inbound_rate_limit,
    )

    now = datetime.now(timezone.utc)
    patient_id = payload.message.patient_id or payload.message.message_id

    # Per-patient inbound rate-limit gate. Fires BEFORE any LLM /
    # handler / DB-write work so a flood doesn't burn tokens or
    # choke the queue. The inbound itself still gets logged after
    # the early-return so we have a forensic trail of what was
    # blocked.
    rate_decision = await check_inbound_rate_limit(
        db, patient_phone=payload.message.phone or patient_id
    )
    if rate_decision.is_limited:
        return await _handle_rate_limited(
            db,
            payload=payload,
            patient_id=patient_id,
            decision=rate_decision,
            now=now,
        )

    # Voice notes: transcribe to text BEFORE any routing, classification,
    # or triage so a spoken message transparently reuses every text flow.
    # We mutate ``payload.message.text`` in place so the workflow, inbox
    # classification, clinical-alert triage, AND the message log all
    # operate on the transcript — not the raw ``[voice-note]`` marker.
    # (A spoken "severe chest pain" must reach clinical triage.) Whisper
    # decode is blocking CPU work, so it runs in a worker thread. The
    # original marker is snapshotted into metadata for forensics, and
    # ``input_kind`` is tagged ``voice`` so the ops inbox can badge it.
    if transcription.looks_like_voice_note(payload.message.text):
        original_marker = payload.message.text
        transcript = await asyncio.to_thread(
            transcription.maybe_transcribe, original_marker
        )
        if transcript:
            payload.message.metadata["voice_marker"] = original_marker
            payload.message.text = transcript
            payload.message.input_kind = "voice"

    last_seen = payload.last_user_message_at or await patient_inbound_repo.get_last_inbound(
        db, patient_id
    )
    if last_seen is None:
        # First time we're hearing from this patient. The current inbound
        # itself opens the 24h customer-service-window for our reply, so
        # treat it as "now" for the policy gate. Otherwise the policy node
        # would force template_required for a brand-new patient mid-flow,
        # and downstream MessageOut validation would 500 on missing template.
        last_seen = now

    # Look up the patient's preferred_language so the LLM compose path
    # can respond in the right language. The compiled graph reads this
    # from the upsert_patient node; the sync fallback needs it passed
    # explicitly. Default to ``en`` when no patient row exists yet.
    patient_pref_lang = "en"
    try:
        if payload.message.phone:
            patient_row = await patients_repo.get_by_phone(
                db, payload.message.phone
            )
            if patient_row is not None and patient_row.preferred_language:
                patient_pref_lang = patient_row.preferred_language
    except Exception:  # noqa: BLE001 — best-effort lookup
        pass

    # Set the LLM tracking context for this request. Every LLM
    # call site downstream reads ``session`` + ``patient_id`` +
    # ``message_id`` from the contextvar so per-call rows in
    # ``llm_call_logs`` get attributed correctly without
    # threading args through every function.
    from services.orchestrator.llm_tracking import (
        set_llm_tracking_context,
    )

    set_llm_tracking_context(
        session=db,
        patient_id=payload.message.phone or patient_id,
        message_id=payload.message.message_id,
    )
    request_started_at = time.monotonic()

    graph = _get_graph(request)
    used_compiled_graph = graph is not None

    if used_compiled_graph:
        final = await _invoke_graph(
            graph,
            message=payload.message,
            patient_id=patient_id,
            last_seen=last_seen,
            now=now,
        )
        intent_str = final.get("intent", "general_question")
        risk_level = final.get("risk_level", "low")
        use_template = bool(final.get("use_template", False))
        policy_reason = final.get("policy_reason", "")
        policy_reason_codes = list(final.get("policy_reason_codes", []))
        flow_action = final.get("flow_action", "ALLOW")
        escalation_required = bool(final.get("escalation_required", False))
        audit_reasons = list(final.get("audit_reasons", []))
        response_body = final.get("response_body", "")
        template_name = final.get("template_name")
        quick_replies = list(final.get("quick_replies", ["CALL", "HELP"]))
        buttons = list(final.get("buttons", []))
        list_rows = list(final.get("list_rows", []))
        list_button_label = final.get("list_button_label")
        list_section_title = final.get("list_section_title")
        ticket_id = final.get("ticket_id")
    else:
        result = await run_agent_workflow(
            message_id=payload.message.message_id,
            patient_id=patient_id,
            text=payload.message.text,
            phone=payload.message.phone,
            last_user_message_at=last_seen,
            now=now,
            preferred_language=patient_pref_lang,
        )
        intent_str = result.intent
        risk_level = result.risk_level
        use_template = result.use_template
        policy_reason = result.policy_reason
        policy_reason_codes = list(result.policy_reason_codes)
        flow_action = result.flow_action
        escalation_required = result.escalation_required
        audit_reasons = list(result.audit_reasons)
        response_body = result.response_body
        template_name = result.template_name
        quick_replies = list(result.quick_replies)
        buttons = []  # sync runner doesn't yet emit interactive buttons
        list_rows = []
        list_button_label = None
        list_section_title = None
        ticket_id = None

    intent = _INTENT_MAP[intent_str]
    decision = PolicyDecision(
        in_customer_service_window=not use_template,
        use_template=use_template,
        reason=policy_reason,
    )

    # Interactive buttons / list rows only render on freeform sends. If
    # policy says we must use a template (outside CSW), drop them silently —
    # the body still goes through, just without tappable controls. Follow-up:
    # register template-with-buttons for outside-window reminders.
    button_models = (
        [
            ActionButton(
                id=str(b.get("id", "")),
                label=str(b.get("label", "")),
                action=str(b.get("action", "")),
            )
            for b in buttons
            if b.get("id") and b.get("label")
        ]
        if not use_template
        else []
    )
    list_row_models = (
        [
            ListRow(
                id=str(r.get("id", "")),
                title=str(r.get("title", "")),
                description=(
                    str(r["description"]) if r.get("description") else None
                ),
            )
            for r in list_rows
            if r.get("id") and r.get("title")
        ]
        if not use_template
        else []
    )

    msg = MessageOut(
        patient_id=patient_id,
        phone=payload.message.phone,
        body=response_body,
        use_template=use_template,
        template_name=template_name,
        quick_replies=[QuickReply(id=reply.lower(), title=reply) for reply in quick_replies],
        buttons=button_models,
        list_rows=list_row_models,
        list_button_label=list_button_label if not use_template else None,
        list_section_title=list_section_title if not use_template else None,
    )

    inbound_log_row = await message_log_repo.append_inbound(
        db,
        patient_id=patient_id,
        payload=payload.message.model_dump(mode="json"),
        occurred_at=now,
    )
    await patient_inbound_repo.set_last_inbound(db, patient_id, now)
    await audit_repo.log_workflow_summary(
        db,
        patient_id=patient_id,
        outbound_mode="TEMPLATE" if use_template else "FREEFORM",
        flow_action=flow_action,
        reason_codes=audit_reasons,
        details={
            "intent": intent_str,
            "risk_level": risk_level,
            "escalation_required": escalation_required,
            "ticket_id": ticket_id,
            "runner": "langgraph" if used_compiled_graph else "sync_fallback",
        },
    )

    # Doctor-inbox annotation. Wrapped — never blocks the response.
    inferred_input_kind = payload.message.input_kind
    if inferred_input_kind is None:
        # Backwards compat for callers (older webhook builds, integration
        # tests) that don't set the field. Sniff based on what the
        # orchestrator's other handlers already use as canonical markers.
        inbound_for_sniff = (payload.message.text or "").lstrip()
        if is_action_tap(inbound_for_sniff):
            inferred_input_kind = "button"
        elif inbound_for_sniff.startswith(
            ("[prescription-upload]", "[wound-photo]")
        ):
            inferred_input_kind = "image"
        else:
            inferred_input_kind = "text"
    request_duration_ms = int(
        (time.monotonic() - request_started_at) * 1000
    )
    inbound_classification_id = await _persist_inbox_classification(
        db,
        message_id=payload.message.message_id,
        patient_phone=patient_id,
        inbound_text=payload.message.text,
        intent_str=intent_str,
        audit_reasons=audit_reasons,
        response_body=response_body,
        escalation_required=escalation_required,
        ticket_id=ticket_id,
        input_kind=inferred_input_kind,
        request_duration_ms=request_duration_ms,
    )
    # Clinical-urgency triage. Independent best-effort path —
    # only writes a row when severity ∈ {high, critical}. Run on
    # freeform text AND transcribed voice notes (``inbound_text`` is the
    # transcript by here) — a spoken "severe chest pain" must page.
    # Action-tap and image inputs are already structured, so skip them.
    if inferred_input_kind in ("text", "voice"):
        await _persist_clinical_alert_if_urgent(
            db,
            inbound_text=payload.message.text,
            intent_str=intent_str,
            patient_phone=patient_id,
            message_log_id=inbound_log_row.id,
            inbound_classification_id=inbound_classification_id,
        )

    return {
        "intent": intent,
        "policy": decision,
        "policy_reason_codes": policy_reason_codes,
        "flow_action": flow_action,
        "risk_level": risk_level,
        "escalation_required": escalation_required,
        "audit_reasons": audit_reasons,
        "ticket_id": ticket_id,
        "runner": "langgraph" if used_compiled_graph else "sync_fallback",
        "message_out": msg,
    }


@app.post("/ops/tickets", response_model=OpsTicketDTO)
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


@app.get("/ops/tickets", response_model=list[OpsTicketDTO])
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


@app.get("/ops/tickets/{ticket_id}", response_model=OpsTicketDTO)
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


@app.post("/ops/tickets/{ticket_id}/ack", response_model=OpsTicketDTO)
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
    await db.commit()
    return _ticket_to_dto(ticket)


@app.post("/ops/tickets/{ticket_id}/resolve", response_model=OpsTicketDTO)
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
    await db.commit()
    return _ticket_to_dto(ticket)


@app.post("/ops/tickets/{ticket_id}/assign", response_model=OpsTicketDTO)
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


@app.post("/ops/tickets/{ticket_id}/snooze", response_model=OpsTicketDTO)
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


@app.post("/ops/tickets/{ticket_id}/unsnooze", response_model=OpsTicketDTO)
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


@app.post("/ops/tickets/{ticket_id}/reopen", response_model=OpsTicketDTO)
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


@app.post("/ops/tickets/{ticket_id}/note", response_model=OpsTicketDTO)
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


@app.get("/ops/dashboard")
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


# ---- Google Calendar webhook push (task #13) --------------------------------


@app.post("/webhooks/google-calendar")
async def google_calendar_webhook(
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Receive a Google Calendar push notification and sync that doctor's
    calendar immediately (push), instead of waiting for the polling sweep.

    Google sends headers — ``X-Goog-Resource-State`` (``sync`` for the initial
    handshake, ``exists`` for a change), ``X-Goog-Channel-Token`` (we set this
    to the doctor_id when registering the watch), ``X-Goog-Channel-ID``. We ACK
    fast (always 200, even on error) so Google doesn't enter aggressive retry;
    the polling sweep remains the backstop. Registering the watch channel
    itself is an ops/setup step (calls Google's events.watch with the doctor_id
    as the token)."""
    state = request.headers.get("X-Goog-Resource-State", "")
    token = request.headers.get("X-Goog-Channel-Token")
    channel_id = request.headers.get("X-Goog-Channel-ID")

    # Initial validation ping when the watch is created — just acknowledge.
    if state == "sync":
        return {"status": "ok", "handshake": True}

    # Verify the channel token. The doctor_id alone is guessable, so when
    # ``GCAL_WEBHOOK_TOKEN`` is configured we require the watch to be
    # registered with a ``{doctor_id}:{secret}`` token and constant-time
    # compare the secret. When unset we fall back to the legacy
    # numeric-doctor-id token (so existing watches keep working) — a warning
    # for that is emitted at startup-config time.
    webhook_secret = os.getenv("GCAL_WEBHOOK_TOKEN", "")
    doctor_id: int | None = None
    if webhook_secret:
        parts = (token or "").split(":", 1)
        if (
            len(parts) == 2
            and parts[0].isdigit()
            and hmac.compare_digest(parts[1], webhook_secret)
        ):
            doctor_id = int(parts[0])
    elif token and token.isdigit():
        doctor_id = int(token)

    if doctor_id is None:
        log.info(
            "calendar webhook: missing/invalid channel token "
            "(channel=%s, state=%s)",
            channel_id,
            state,
        )
        return {"status": "ok", "synced": False, "reason": "bad_token"}

    from services.scheduler import calendar_sync_sweep
    try:
        result = await calendar_sync_sweep.reconcile_doctor(
            db, doctor_id=doctor_id
        )
        await db.commit()
    except Exception:  # noqa: BLE001 — never make Google retry on our error
        log.exception(
            "calendar webhook sync failed for doctor %s", doctor_id
        )
        return {"status": "ok", "synced": False, "reason": "sync_error"}

    return {
        "status": "ok",
        "synced": True,
        "doctor_id": doctor_id,
        "changes": result.get("changes_received", 0),
    }

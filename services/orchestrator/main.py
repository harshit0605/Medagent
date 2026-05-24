from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AppointmentStatus,
    Doctor,
    DoctorOAuthStatus,
    OpsTicket,
    RecapStatus,
)
from app.db.repositories import adherence_events as adherence_events_repo
from app.db.repositories import appointment_recaps as appointment_recaps_repo
from app.db.repositories import appointments as appointments_repo
from app.db.repositories import audit as audit_repo
from app.db.repositories import (
    inbound_classifications as inbound_classifications_repo,
)
from app.db.repositories import care_plan_exemptions as care_plan_exemptions_repo
from app.db.repositories import care_plans as care_plans_repo
from app.db.repositories import caregivers as caregivers_repo
from app.db.repositories import cohort_tags as cohort_tags_repo
from app.db.repositories import dashboard as dashboard_repo
from app.db.repositories import delivery_metrics as delivery_metrics_repo
from app.db.repositories import doctors as doctors_repo
from app.db.repositories import lab_followups as lab_followups_repo
from app.db.repositories import message_log as message_log_repo
from app.db.repositories import ops_tickets as ops_tickets_repo
from app.db.repositories import patient_inbound as patient_inbound_repo
from app.db.repositories import patients as patients_repo
from app.db.repositories import regimens as regimens_repo
from app.db.repositories import scheduled_events as scheduled_events_repo
from app.db.session import get_session
from services.orchestrator import google_calendar as gcal
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
from services.orchestrator.routers import households as households_router
from services.orchestrator.routers import lab_followups as lab_followups_router
from services.orchestrator.routers import llm_cost_analytics as llm_cost_analytics_router
from services.orchestrator.routers import ops_analytics as ops_analytics_router
from services.orchestrator.routers import ops_health as ops_health_router
from services.orchestrator.routers import orders as orders_router
from services.orchestrator.routers import post_op as post_op_router
from services.orchestrator.routers import pregnancy as pregnancy_router
from services.orchestrator.routers import prescriptions as prescriptions_router
from services.orchestrator.routers import regimen as regimen_router
from services.orchestrator.routers import side_effect_analytics as side_effect_analytics_router
from services.orchestrator.routers import visit_briefs as visit_briefs_router
from services.orchestrator.routers._dtos import (
    LabFollowupDTO,
    RegimenDTO,
    _lab_to_dto,
    _regimen_to_dto,
)
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


# ---- Doctor / Google Calendar endpoints --------------------------------------


class DoctorCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    email: str = Field(min_length=3, max_length=255)
    phone: str | None = None
    timezone: str = Field(default="UTC", max_length=64)
    calendar_id: str = Field(default="primary", max_length=255)


class DoctorDTO(BaseModel):
    id: int
    name: str
    email: str
    phone: str | None = None
    timezone: str
    calendar_id: str
    oauth_status: str
    google_user_id: str | None = None
    # Inbound calendar-sync state. ``gcal_last_synced_at`` is the
    # wall-clock of the most recent successful incremental pass;
    # the UI renders "synced N min ago" off this so ops sees the
    # sweep is working without querying logs.
    gcal_last_synced_at: datetime | None = None
    is_on_call: bool = False
    created_at: datetime
    updated_at: datetime


def _doctor_to_dto(row: Doctor) -> DoctorDTO:
    last_synced = getattr(row, "gcal_last_synced_at", None)
    if last_synced is not None and last_synced.tzinfo is None:
        last_synced = last_synced.replace(tzinfo=timezone.utc)
    return DoctorDTO(
        id=row.id,
        name=row.name,
        email=row.email,
        phone=row.phone,
        timezone=row.timezone,
        calendar_id=row.calendar_id,
        oauth_status=row.oauth_status.value,
        google_user_id=row.google_user_id,
        gcal_last_synced_at=last_synced,
        is_on_call=bool(getattr(row, "is_on_call", False)),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@app.post("/doctors", response_model=DoctorDTO)
async def create_doctor(
    payload: DoctorCreateRequest, db: AsyncSession = Depends(get_session)
) -> DoctorDTO:
    existing = await doctors_repo.get_by_email(db, payload.email)
    if existing is not None:
        raise HTTPException(status_code=409, detail="doctor with this email already exists")
    row = await doctors_repo.create(
        db,
        name=payload.name,
        email=payload.email,
        phone=payload.phone,
        timezone_name=payload.timezone,
        calendar_id=payload.calendar_id,
    )
    return _doctor_to_dto(row)


@app.get("/doctors", response_model=list[DoctorDTO])
async def list_doctors(db: AsyncSession = Depends(get_session)) -> list[DoctorDTO]:
    rows = await doctors_repo.list_all(db)
    return [_doctor_to_dto(r) for r in rows]


@app.get("/doctors/{doctor_id}", response_model=DoctorDTO)
async def get_doctor(
    doctor_id: int, db: AsyncSession = Depends(get_session)
) -> DoctorDTO:
    row = await doctors_repo.get(db, doctor_id)
    if row is None:
        raise HTTPException(status_code=404, detail="doctor not found")
    return _doctor_to_dto(row)


class DoctorOnCallRequest(BaseModel):
    on_call: bool


@app.post(
    "/doctors/{doctor_id}/on-call", response_model=DoctorDTO
)
async def set_doctor_on_call(
    doctor_id: int,
    body: DoctorOnCallRequest,
    db: AsyncSession = Depends(get_session),
) -> DoctorDTO:
    """Toggle the doctor's ``is_on_call`` flag. Doctors flagged
    on-call appear in the critical-alert paging fallback when
    a patient has no primary-doctor history. A doctor without
    a ``phone`` can still be flagged but won't actually
    receive pages — surfaced as a UI warning rather than
    rejected here, since the flag is also used by future
    rota-aware features."""
    row = await doctors_repo.set_on_call(
        db, doctor_id, on_call=body.on_call
    )
    if row is None:
        raise HTTPException(status_code=404, detail="doctor not found")
    # Build the DTO BEFORE commit. SQLAlchemy expires all row
    # attributes after commit by default; accessing them then
    # triggers a lazy reload that races connection-cleanup on
    # async sessions and surfaces as MissingGreenlet errors in
    # tests. Building from the still-attached row sidesteps it.
    dto = _doctor_to_dto(row)
    await db.commit()
    return dto


@app.post("/doctors/{doctor_id}/disconnect", response_model=DoctorDTO)
async def disconnect_doctor(
    doctor_id: int, db: AsyncSession = Depends(get_session)
) -> DoctorDTO:
    row = await doctors_repo.mark_disconnected(
        db, doctor_id, status=DoctorOAuthStatus.disconnected
    )
    if row is None:
        raise HTTPException(status_code=404, detail="doctor not found")
    return _doctor_to_dto(row)


class OAuthCallbackRequest(BaseModel):
    """Posted by the Next.js OAuth callback after exchanging code → tokens."""

    code: str = Field(min_length=1)
    redirect_uri: str = Field(min_length=1)


@app.post("/doctors/{doctor_id}/oauth/callback", response_model=DoctorDTO)
async def doctor_oauth_callback(
    doctor_id: int,
    payload: OAuthCallbackRequest,
    db: AsyncSession = Depends(get_session),
) -> DoctorDTO:
    """Internal endpoint — Next.js calls this with the freshly issued auth code.

    We exchange it server-to-server (so the client secret never touches the
    browser), then persist the encrypted refresh token + cached access token.
    """
    row = await doctors_repo.get(db, doctor_id)
    if row is None:
        raise HTTPException(status_code=404, detail="doctor not found")
    try:
        token_payload = await gcal.exchange_code_for_tokens(
            code=payload.code, redirect_uri=payload.redirect_uri
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"google token exchange failed: {exc}") from exc

    refresh_token = token_payload.get("refresh_token")
    if not refresh_token:
        # Google only issues a refresh_token on the first consent. If we didn't
        # get one, the user previously authorized this client without
        # access_type=offline / prompt=consent.
        raise HTTPException(
            status_code=400,
            detail=(
                "Google did not return a refresh_token. Visit "
                "https://myaccount.google.com/permissions, revoke this app, "
                "then run Connect again."
            ),
        )

    access_token = token_payload["access_token"]
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=int(token_payload.get("expires_in", 3600))
    )
    scopes = token_payload.get("scope", "")

    google_user_id: str | None = None
    id_token = token_payload.get("id_token")
    if id_token:
        # The id_token is a JWT — decode the middle segment to read `sub`.
        # No verification needed; we got it directly from Google over TLS.
        import base64
        import json as _json

        try:
            parts = id_token.split(".")
            padding = "=" * (-len(parts[1]) % 4)
            claims = _json.loads(base64.urlsafe_b64decode(parts[1] + padding))
            google_user_id = claims.get("sub")
        except Exception:
            log.warning("id_token claim parse failed; storing without google_user_id")

    updated = await doctors_repo.store_oauth_tokens(
        db,
        doctor_id,
        refresh_token=refresh_token,
        access_token=access_token,
        access_token_expires_at=expires_at,
        scopes=scopes,
        google_user_id=google_user_id,
    )
    assert updated is not None
    return _doctor_to_dto(updated)


# ---- Calendar tooling exposed as endpoints (for ops console + tests) --------


class TimeSlotDTO(BaseModel):
    start: datetime
    end: datetime


class AvailabilityDTO(BaseModel):
    doctor_id: int
    timezone: str
    requested_window: TimeSlotDTO
    duration_minutes: int
    busy: list[TimeSlotDTO]
    free: list[TimeSlotDTO]


class CalendarEventDTO(BaseModel):
    event_id: str
    calendar_id: str
    summary: str | None = None
    description: str | None = None
    start: datetime
    end: datetime
    html_link: str | None = None
    attendees: list[str] = Field(default_factory=list)
    status: str | None = None


# ---- Doctor daily digest -------------------------------------------------


class DigestAppointmentDTO(BaseModel):
    appointment_id: int
    patient_id: int
    patient_full_name: str
    scheduled_for: datetime
    end_at: datetime
    status: str
    summary: str | None
    has_pending_recap: bool


class DigestRecapDraftDTO(BaseModel):
    recap_id: int
    appointment_id: int
    patient_id: int
    patient_full_name: str
    appointment_date: datetime
    created_at: datetime


class DigestTicketDTO(BaseModel):
    ticket_id: int
    patient_id: int | None
    patient_full_name: str | None
    patient_phone: str
    category: str
    priority: str
    status: str
    sla_minutes: int
    created_at: datetime
    sla_breached_at: datetime | None


class DigestSideEffectDTO(BaseModel):
    ticket_id: int
    patient_id: int | None
    patient_full_name: str | None
    created_at: datetime
    status: str
    reported_text: str | None


class DoctorDigestDTO(BaseModel):
    doctor_id: int
    doctor_name: str
    when: datetime
    summary_counts: dict[str, int]
    appointments_today: list[DigestAppointmentDTO]
    recap_drafts_pending: list[DigestRecapDraftDTO]
    side_effect_reports_24h: list[DigestSideEffectDTO]
    open_tickets: list[DigestTicketDTO]


@app.get(
    "/doctors/{doctor_id}/daily-digest",
    response_model=DoctorDigestDTO,
)
async def get_doctor_daily_digest(
    doctor_id: int,
    when: datetime | None = None,
    db: AsyncSession = Depends(get_session),
) -> DoctorDigestDTO:
    """Doctor's morning panel-management view. Aggregates today's
    appointments, pending recap drafts, recent side-effect
    reports, and open high-priority tickets for the doctor's
    panel into a single read-only DTO so the doctor can scan
    their day in 5 seconds.

    ``when`` defaults to "now in UTC". Pass an explicit tz-aware
    datetime to align the "today" window to a doctor's local
    timezone (most clinics will compute their local-day window
    client-side and pass UTC bounds in)."""
    from services.orchestrator import doctor_digest as digest_module

    doctor = await doctors_repo.get(db, doctor_id)
    if doctor is None:
        raise HTTPException(status_code=404, detail="doctor not found")

    digest = await digest_module.build_digest(
        db,
        doctor_id=doctor_id,
        doctor_name=doctor.name,
        when=when,
    )
    return DoctorDigestDTO(
        doctor_id=digest.doctor_id,
        doctor_name=digest.doctor_name,
        when=digest.when,
        summary_counts=digest.summary_counts,
        appointments_today=[
            DigestAppointmentDTO(
                appointment_id=a.appointment_id,
                patient_id=a.patient_id,
                patient_full_name=a.patient_full_name,
                scheduled_for=a.scheduled_for,
                end_at=a.end_at,
                status=a.status,
                summary=a.summary,
                has_pending_recap=a.has_pending_recap,
            )
            for a in digest.appointments_today
        ],
        recap_drafts_pending=[
            DigestRecapDraftDTO(
                recap_id=r.recap_id,
                appointment_id=r.appointment_id,
                patient_id=r.patient_id,
                patient_full_name=r.patient_full_name,
                appointment_date=r.appointment_date,
                created_at=r.created_at,
            )
            for r in digest.recap_drafts_pending
        ],
        side_effect_reports_24h=[
            DigestSideEffectDTO(
                ticket_id=s.ticket_id,
                patient_id=s.patient_id,
                patient_full_name=s.patient_full_name,
                created_at=s.created_at,
                status=s.status,
                reported_text=s.reported_text,
            )
            for s in digest.side_effect_reports_24h
        ],
        open_tickets=[
            DigestTicketDTO(
                ticket_id=t.ticket_id,
                patient_id=t.patient_id,
                patient_full_name=t.patient_full_name,
                patient_phone=t.patient_phone,
                category=t.category,
                priority=t.priority,
                status=t.status,
                sla_minutes=t.sla_minutes,
                created_at=t.created_at,
                sla_breached_at=t.sla_breached_at,
            )
            for t in digest.open_tickets
        ],
    )


@app.get("/doctors/{doctor_id}/availability", response_model=AvailabilityDTO)
async def get_doctor_availability(
    doctor_id: int,
    start: datetime,
    end: datetime,
    duration_minutes: int = 30,
    db: AsyncSession = Depends(get_session),
) -> AvailabilityDTO:
    if start >= end:
        raise HTTPException(status_code=400, detail="end must be after start")
    if duration_minutes < 5 or duration_minutes > 480:
        raise HTTPException(status_code=400, detail="duration_minutes must be 5..480")
    try:
        result = await gcal.find_slots(
            db,
            doctor_id=doctor_id,
            window=gcal.TimeSlot(start=start, end=end),
            duration_minutes=duration_minutes,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return AvailabilityDTO(
        doctor_id=result.doctor_id,
        timezone=result.timezone,
        requested_window=TimeSlotDTO(**result.requested_window.model_dump()),
        duration_minutes=result.duration_minutes,
        busy=[TimeSlotDTO(**b.model_dump()) for b in result.busy],
        free=[TimeSlotDTO(**f.model_dump()) for f in result.free],
    )


class BookAppointmentRequest(BaseModel):
    start: datetime
    end: datetime
    summary: str = Field(min_length=1, max_length=255)
    description: str | None = None
    patient_email: str | None = None
    patient_phone: str | None = None
    extra_attendees: list[str] = Field(default_factory=list)


@app.post(
    "/doctors/{doctor_id}/appointments",
    response_model=CalendarEventDTO,
)
async def book_doctor_appointment(
    doctor_id: int,
    payload: BookAppointmentRequest,
    db: AsyncSession = Depends(get_session),
) -> CalendarEventDTO:
    if payload.start >= payload.end:
        raise HTTPException(status_code=400, detail="end must be after start")
    try:
        event = await gcal.book_slot(
            db,
            doctor_id=doctor_id,
            start=payload.start,
            end=payload.end,
            summary=payload.summary,
            description=payload.description,
            patient_email=payload.patient_email,
            patient_phone=payload.patient_phone,
            attendees=payload.extra_attendees,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return CalendarEventDTO(**event.model_dump())


@app.delete("/doctors/{doctor_id}/appointments/{event_id}", status_code=204)
async def cancel_doctor_appointment(
    doctor_id: int,
    event_id: str,
    db: AsyncSession = Depends(get_session),
) -> None:
    try:
        await gcal.cancel_event(db, doctor_id=doctor_id, event_id=event_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


class AppointmentDTO(BaseModel):
    id: int
    patient_id: int
    doctor_id: int
    doctor_name: str | None = None
    doctor_timezone: str | None = None
    patient_full_name: str | None = None
    scheduled_for: datetime
    end_at: datetime
    status: str
    summary: str | None = None
    notes: str | None = None
    calendar_html_link: str | None = None


@app.get("/appointments/{appointment_id}", response_model=AppointmentDTO)
async def get_appointment(
    appointment_id: int, db: AsyncSession = Depends(get_session)
) -> AppointmentDTO:
    appointment = await appointments_repo.get(db, appointment_id)
    if appointment is None:
        raise HTTPException(status_code=404, detail="appointment not found")
    doctor = await doctors_repo.get(db, appointment.doctor_id)
    patient = await patients_repo.get(db, appointment.patient_id)
    return AppointmentDTO(
        id=appointment.id,
        patient_id=appointment.patient_id,
        doctor_id=appointment.doctor_id,
        doctor_name=doctor.name if doctor else None,
        doctor_timezone=doctor.timezone if doctor else None,
        patient_full_name=patient.full_name if patient else None,
        scheduled_for=appointment.scheduled_for,
        end_at=appointment.end_at,
        status=appointment.status.value,
        summary=appointment.summary,
        notes=appointment.notes,
        calendar_html_link=appointment.calendar_html_link,
    )


# ---- Pre-visit summary (doctor brief) -------------------------------------


class PreVisitInboxItemDTO(BaseModel):
    id: int
    created_at: datetime
    category: str
    urgency: str
    summary: str | None
    inbound_text: str | None
    handler_used: str | None
    escalated: bool


class PreVisitTicketDTO(BaseModel):
    ticket_id: str
    category: str
    priority: str
    status: str
    is_overdue: bool
    is_snoozed: bool
    created_at: datetime
    # First-cross SLA breach marker — surfaced on the doctor's
    # pre-visit brief so they can see "this care issue went past
    # SLA before it was resolved" at a glance.
    sla_breached_at: datetime | None = None


class PreVisitCohortTagDTO(BaseModel):
    cohort_tag_id: int
    label: str
    slug: str


class PreVisitExemptionDTO(BaseModel):
    id: int
    care_plan_id: int
    care_plan_test_name: str | None
    reason: str
    expires_at: datetime | None


class PreVisitRecapExcerptDTO(BaseModel):
    """Just enough of the most recent SENT recap so the doctor can
    skim what the previous visit landed on. Detail is one click away
    via /appointments/{id}/recap."""

    appointment_id: int
    appointment_date: datetime
    summary: str  # generated_text trimmed to ~600 chars
    status: str  # sent / acknowledged / questioned


class PreVisitSummaryDTO(BaseModel):
    appointment: AppointmentDTO
    patient: PatientSummaryDTO
    cohort_flags: list[str]  # legacy boolean cohorts the patient is in
    cohort_tags: list[PreVisitCohortTagDTO]
    regimens: list["RegimenDTO"]
    adherence: AdherenceSummaryDTO
    open_lab_followups: list[LabFollowupDTO]
    open_tickets: list[PreVisitTicketDTO]
    active_exemptions: list[PreVisitExemptionDTO]
    recent_inbox: list[PreVisitInboxItemDTO]
    last_recap: PreVisitRecapExcerptDTO | None
    has_caregiver_cc: bool


@app.get(
    "/appointments/{appointment_id}/pre-visit",
    response_model=PreVisitSummaryDTO,
)
async def get_pre_visit_summary(
    appointment_id: int, db: AsyncSession = Depends(get_session)
) -> PreVisitSummaryDTO:
    """Doctor-facing pre-visit brief. Aggregates the patient's regimens,
    30-day adherence, recent classified inbox messages, open lab
    followups, open ops tickets, cohort tags + flags, active care-plan
    exemptions, and the most recent prior visit's recap excerpt — so
    the doctor walks in informed without clicking through five
    different ops console screens.

    Read-only; no clinical decisions surfaced — just the data."""
    appointment = await appointments_repo.get(db, appointment_id)
    if appointment is None:
        raise HTTPException(status_code=404, detail="appointment not found")

    doctor = await doctors_repo.get(db, appointment.doctor_id)
    patient = await patients_repo.get(db, appointment.patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="patient row missing")

    appointment_dto = AppointmentDTO(
        id=appointment.id,
        patient_id=appointment.patient_id,
        doctor_id=appointment.doctor_id,
        doctor_name=doctor.name if doctor else None,
        doctor_timezone=doctor.timezone if doctor else None,
        patient_full_name=patient.full_name,
        scheduled_for=appointment.scheduled_for,
        end_at=appointment.end_at,
        status=appointment.status.value,
        summary=appointment.summary,
        notes=appointment.notes,
        calendar_html_link=appointment.calendar_html_link,
    )

    # Regimens — fetched once, reused for both the regimen list AND
    # the patient_summary header's active_regimen_count.
    today = datetime.now(timezone.utc).date()
    active_regimens = await regimens_repo.list_for_patient(
        db, patient.id, active_on=today
    )
    regimen_dtos = [_regimen_to_dto(r) for r in active_regimens]

    # Patient summary card — minimal flat shape matching the /patients
    # listing so the page can reuse the standard header component.
    upcoming_appts = await appointments_repo.list_for_patient(
        db, patient.id, upcoming_only=True, limit=5
    )
    upcoming_confirmed = [
        a
        for a in upcoming_appts
        if a.status == AppointmentStatus.confirmed
    ]
    open_tickets_count_rows = (
        await ops_tickets_repo.list_for_patient(db, patient.phone)
        if patient.phone
        else []
    )
    open_ticket_count = sum(
        1 for t in open_tickets_count_rows if t.status.value == "open"
    )
    patient_summary_dto = PatientSummaryDTO(
        id=patient.id,
        full_name=patient.full_name,
        phone=patient.phone,
        cohort_diabetes=patient.cohort_diabetes,
        cohort_cardiac=patient.cohort_cardiac,
        cohort_fall_risk=patient.cohort_fall_risk,
        active_regimen_count=len(active_regimens),
        upcoming_appointment_count=len(upcoming_confirmed),
        open_ticket_count=open_ticket_count,
        created_at=patient.created_at,
    )

    # 30-day adherence.
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    adh_events = await adherence_events_repo.list_for_patient(
        db, patient.id, since=cutoff
    )
    adherence_dto = _adherence_summary(adh_events, window_days=30)

    # Open lab followups.
    all_labs = await lab_followups_repo.list_for_patient(db, patient.id)
    open_labs = [
        _lab_to_dto(lab)
        for lab in all_labs
        if lab.status.value in ("due", "booked")
    ]

    # Open ops tickets — by phone (the patient_id used in ops_tickets).
    open_tickets_rows = (
        await ops_tickets_repo.list_for_patient(db, patient.phone)
        if patient.phone
        else []
    )
    now = datetime.now(timezone.utc)
    open_tickets: list[PreVisitTicketDTO] = []
    for t in open_tickets_rows:
        if t.status.value not in ("open", "acknowledged"):
            continue
        created = t.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        snoozed_until = t.snoozed_until
        if snoozed_until is not None and snoozed_until.tzinfo is None:
            snoozed_until = snoozed_until.replace(tzinfo=timezone.utc)
        is_snoozed = snoozed_until is not None and snoozed_until > now
        is_overdue = (
            not is_snoozed
            and (created + timedelta(minutes=t.sla_minutes)) <= now
        )
        sla_breached_at = t.sla_breached_at
        if sla_breached_at is not None and sla_breached_at.tzinfo is None:
            sla_breached_at = sla_breached_at.replace(tzinfo=timezone.utc)
        open_tickets.append(
            PreVisitTicketDTO(
                ticket_id=str(t.id),
                category=t.category,
                priority=t.priority,
                status=t.status.value,
                is_overdue=is_overdue,
                is_snoozed=is_snoozed,
                created_at=t.created_at,
                sla_breached_at=sla_breached_at,
            )
        )

    # Active care-plan exemptions (joined with plan info for the test name).
    exemptions_with_plan = (
        await care_plan_exemptions_repo.list_with_plan_info(
            db, patient.id, include_inactive=False
        )
    )
    exemption_dtos = [
        PreVisitExemptionDTO(
            id=ex.id,
            care_plan_id=ex.care_plan_id,
            care_plan_test_name=plan.test_name if plan else None,
            reason=ex.reason,
            expires_at=ex.expires_at,
        )
        for ex, plan in exemptions_with_plan
    ]

    # Cohort tags.
    tag_assignments = await cohort_tags_repo.list_for_patient(db, patient.id)
    tag_dtos = [
        PreVisitCohortTagDTO(
            cohort_tag_id=tag.id, label=tag.label, slug=tag.slug
        )
        for _assignment, tag in tag_assignments
    ]
    cohort_flags = [
        attr
        for attr in care_plans_repo.KNOWN_COHORT_ATTRS
        if getattr(patient, attr, False)
    ]

    # Recent inbox — last 10 freeform classifications, newest first.
    if patient.phone:
        inbox_rows = await inbound_classifications_repo.list_recent(
            db, patient_phone=patient.phone, limit=10
        )
    else:
        inbox_rows = []
    inbox_dtos = [
        PreVisitInboxItemDTO(
            id=r.id,
            created_at=r.created_at,
            category=r.category,
            urgency=r.urgency,
            summary=r.summary,
            inbound_text=r.inbound_text,
            handler_used=r.handler_used,
            escalated=r.escalated,
        )
        for r in inbox_rows
    ]

    # Most recent SENT recap on a different appointment (doctor wants
    # the PRIOR visit's recap, not this appointment's recap-in-progress).
    last_recap_dto: PreVisitRecapExcerptDTO | None = None
    prior_recaps = await appointment_recaps_repo.list_for_patient(
        db, patient.id, limit=20
    )
    for recap in prior_recaps:
        if recap.appointment_id == appointment.id:
            continue
        if recap.status == RecapStatus.draft:
            continue
        if not recap.generated_text:
            continue
        prior_appt = await appointments_repo.get(db, recap.appointment_id)
        if prior_appt is None:
            continue
        excerpt = recap.generated_text.strip()
        if len(excerpt) > 600:
            excerpt = excerpt[:597].rstrip() + "…"
        last_recap_dto = PreVisitRecapExcerptDTO(
            appointment_id=recap.appointment_id,
            appointment_date=prior_appt.scheduled_for,
            summary=excerpt,
            status=recap.status.value,
        )
        break

    # Caregiver cc state — bool flag for the header (full list is on
    # patient detail).
    caregivers = await caregivers_repo.list_active_recap_recipients(
        db, patient.id
    )
    has_caregiver_cc = len(caregivers) > 0

    return PreVisitSummaryDTO(
        appointment=appointment_dto,
        patient=patient_summary_dto,
        cohort_flags=cohort_flags,
        cohort_tags=tag_dtos,
        regimens=regimen_dtos,
        adherence=adherence_dto,
        open_lab_followups=open_labs,
        open_tickets=open_tickets,
        active_exemptions=exemption_dtos,
        recent_inbox=inbox_dtos,
        last_recap=last_recap_dto,
        has_caregiver_cc=has_caregiver_cc,
    )


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


# ---- Patient list + detail endpoints (powers the ops console) ---------------


class PatientSummaryDTO(BaseModel):
    """Lightweight row for the patients list page."""

    id: int
    full_name: str
    phone: str
    cohort_diabetes: bool
    cohort_cardiac: bool
    cohort_fall_risk: bool
    active_regimen_count: int
    upcoming_appointment_count: int
    open_ticket_count: int
    created_at: datetime


class AdherenceSummaryDTO(BaseModel):
    window_days: int
    total: int
    taken: int            # all takens (on-time + late)
    taken_on_time: int    # tapped Taken inside the grace window
    taken_late: int       # tapped Mark-as-taken after sweep marked missed
    missed: int
    skipped: int
    delayed: int
    scheduled: int
    adherence_rate: float  # taken / (taken + missed + skipped); 0..1
    on_time_rate: float    # taken_on_time / taken; 0..1 (NaN-safe → 0.0)


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


def _extract_reported_text(notes: str | None) -> str | None:
    """Pull the verbatim patient-said block out of a side_effect_report
    ticket's notes. The handler writes notes in this shape::

        [side-effect report]
        Reported at: 2026-05-07T22:00:00+00:00

        Patient said:
          > metformin gave me severe headaches

        Active regimens at time of report:
          - Metformin 500 mg

    The "Patient said:" block is what the doctor cares about —
    extracting it for the DTO keeps the UI clean (no need to
    parse multi-line markdown on the frontend) and centralises the
    notes-format coupling here so a future migration to a
    structured ``inbound_text`` column only touches this helper.

    Returns None when the block is missing (legacy tickets, manually
    created tickets, future format changes) — the UI falls back to
    rendering raw notes in that case rather than crashing.
    """
    if not notes:
        return None
    marker = "Patient said:"
    idx = notes.find(marker)
    if idx == -1:
        return None
    after = notes[idx + len(marker) :]
    lines: list[str] = []
    # Block ends at first blank line OR first line that doesn't
    # start with the ``  > `` marker we use for the verbatim quote.
    for raw in after.splitlines():
        stripped = raw.strip()
        if not stripped:
            if lines:
                break  # blank after some content → end of block
            continue  # blank before content → skip
        # Strip the leading "> " quote marker from each line.
        if stripped.startswith(">"):
            lines.append(stripped.lstrip(">").strip())
        else:
            # First non-quote line ends the block.
            break
    if not lines:
        return None
    return "\n".join(lines).strip() or None


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


def _adherence_summary(
    events: list[Any], window_days: int = 30
) -> AdherenceSummaryDTO:
    counts = {"taken": 0, "missed": 0, "skipped": 0, "delayed": 0, "scheduled": 0}
    taken_late = 0
    for e in events:
        v = e.status.value if hasattr(e.status, "value") else str(e.status)
        if v in counts:
            counts[v] += 1
        if v == "taken":
            metadata = getattr(e, "confirmation_metadata", None) or {}
            if metadata.get("late_confirmed"):
                taken_late += 1
    completed = counts["taken"] + counts["missed"] + counts["skipped"]
    rate = (counts["taken"] / completed) if completed else 0.0
    on_time = counts["taken"] - taken_late
    on_time_rate = (on_time / counts["taken"]) if counts["taken"] else 0.0
    return AdherenceSummaryDTO(
        window_days=window_days,
        total=len(events),
        taken=counts["taken"],
        taken_on_time=on_time,
        taken_late=taken_late,
        missed=counts["missed"],
        skipped=counts["skipped"],
        delayed=counts["delayed"],
        scheduled=counts["scheduled"],
        adherence_rate=round(rate, 3),
        on_time_rate=round(on_time_rate, 3),
    )


@app.get("/patients", response_model=list[PatientSummaryDTO])
async def list_patients(
    db: AsyncSession = Depends(get_session), limit: int = 200
) -> list[PatientSummaryDTO]:
    from app.db.models import AppointmentStatus

    rows = await patients_repo.list_all(db, limit=limit)
    today = datetime.now(timezone.utc).date()
    out: list[PatientSummaryDTO] = []
    for p in rows:
        regimens = await regimens_repo.list_for_patient(db, p.id, active_on=today)
        appts_raw = await appointments_repo.list_for_patient(
            db, p.id, upcoming_only=True, limit=5
        )
        # Only confirmed upcoming — cancelled / no_show shouldn't inflate the count.
        appts = [a for a in appts_raw if a.status == AppointmentStatus.confirmed]
        # Open ops tickets for this patient (by phone — that's the ticket's
        # patient_id field).
        open_tickets = [
            t
            for t in await ops_tickets_repo.list_tickets(db, status="open")
            if t.patient_id == p.phone
        ]
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
                open_ticket_count=len(open_tickets),
                created_at=p.created_at,
            )
        )
    return out


@app.get("/patients/{patient_id}", response_model=PatientDetailDTO)
async def get_patient_detail(
    patient_id: int, db: AsyncSession = Depends(get_session)
) -> PatientDetailDTO:
    p = await patients_repo.get(db, patient_id)
    if p is None:
        raise HTTPException(status_code=404, detail="patient not found")

    regimens = await regimens_repo.list_for_patient(db, patient_id)

    from app.db.models import AppointmentStatus

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


@app.get(
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


# ---- Patient language + onboarding reset -----------------------------------


class PatientLanguageUpdateRequest(BaseModel):
    preferred_language: str = Field(min_length=2, max_length=8)


@app.get("/i18n/languages", response_model=list[dict])
async def list_supported_languages() -> list[dict]:
    """Static allowlist used by the patient-detail language picker.
    Mirrors ``app/i18n.py::SUPPORTED_LANGUAGES`` so adding a language
    is a constants-only change visible in the UI immediately."""
    from app import i18n

    return [
        {"code": opt.code, "label": opt.label}
        for opt in i18n.SUPPORTED_LANGUAGES
    ]


@app.put(
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


@app.post("/patients/{patient_id}/reset-onboarding", response_model=PatientDetailDTO)
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


@app.post(
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
    await db.commit()
    return await get_patient_detail(patient_id, db)


@app.post(
    "/patients/{patient_id}/unpause-bot",
    response_model=PatientDetailDTO,
)
async def unpause_bot_endpoint(
    patient_id: int,
    db: AsyncSession = Depends(get_session),
) -> PatientDetailDTO:
    """Clear the ops-initiated bot pause. Outbound resumes on the
    next dispatcher tick. Patient is not notified — pause / unpause
    are invisible by design."""
    row = await patients_repo.unpause_bot(db, patient_id)
    if row is None:
        raise HTTPException(status_code=404, detail="patient not found")
    await db.commit()
    return await get_patient_detail(patient_id, db)


class PatientErasureRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=255)
    confirm: bool = False


@app.post("/patients/{patient_id}/erase")
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
    await db.commit()

    return {
        "patient_id": patient.id,
        "erased_at": patient.erased_at.isoformat()
        if patient.erased_at
        else None,
        "anonymized_phone": patient.phone,
    }


@app.get("/patients/{patient_id}/export")
async def export_patient_data(
    patient_id: int,
    actor: str = "ops",
    window_days: int = 365,
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

    Parameters:
        actor: who's running the export. Goes into the audit row.
            Defaults to ``"ops"`` for ops-console-driven exports;
            an automated re-import or scheduled job should pass
            its own identifier.
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
    if not actor or len(actor) > 128:
        raise HTTPException(
            status_code=400, detail="actor required (max 128 chars)"
        )

    document = await patient_export.build_patient_export(
        db,
        patient_id=patient_id,
        actor=actor,
        window_days=window_days,
    )
    if document is None:
        raise HTTPException(status_code=404, detail="patient not found")
    await db.commit()
    return document


# Resolve PatientDetailDTO's forward-reference to RegimenDTO (imported from
# routers._dtos). Without this Pydantic v2 raises on first instantiation.
PatientDetailDTO.model_rebuild()

from __future__ import annotations

"""LangGraph-aligned agent workflow for Medagent orchestrator (async).

This module follows current agent best practices:
- strongly-typed shared state
- deterministic routing for safety/policy decisions
- explicit human escalation branch
- graph compilation with optional checkpointer support
- pure-Python async fallback runner when LangGraph is unavailable

LLM integration: ``_detect_intent``, ``_risk_triage`` and ``_compose`` each
consult :mod:`services.orchestrator.llm` first and fall back to deterministic
keyword logic on any failure (missing key, timeout, malformed output, or
``LLM_ENABLED=0``). The safety floor is ALWAYS the rule-based decision — the
LLM may only escalate severity, never reduce it.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Any, Awaitable, Callable, Literal, TypedDict

from services.orchestrator.llm import get_llm
from services.orchestrator.policy_gate import (
    AuditTrail,
    PatientStateStore,
    PolicyDecision,
    PolicyGate,
)

# add_messages is the standard LangGraph reducer for accumulating chat messages
# across turns within a per-patient thread. Falls back to a no-op append when
# langgraph is not installed (so unit tests that bypass the graph still work).
try:
    from langgraph.graph.message import add_messages  # type: ignore
except Exception:  # pragma: no cover — langgraph is in our deps
    def add_messages(left: list, right: list) -> list:  # type: ignore[no-redef]
        return list(left or []) + list(right or [])


Intent = Literal[
    "adherence_update",
    "refill_request",
    "symptom_report",
    "pregnancy_checklist",
    "followup_update",
    "booking_request",
    "general_question",
]
RiskLevel = Literal["low", "medium", "high", "critical"]


class AgentState(TypedDict, total=False):
    message_id: str
    patient_id: str            # WhatsApp wa_id (phone digits only)
    patient_db_id: int | None  # FK into patients table (set by upsert_patient node)
    # ISO-639-1 code from patients.preferred_language. Threaded into
    # the LLM compose path so replies match the patient's language.
    # Set on upsert_patient; defaults to ``en`` for fresh patients.
    preferred_language: str
    phone: str | None
    text: str
    now_utc: datetime
    last_user_message_at: datetime | None
    in_customer_service_window: bool
    use_template: bool
    policy_reason: str
    policy_reason_codes: list[str]
    flow_action: str
    intent: Intent
    risk_level: RiskLevel
    escalation_required: bool
    escalation_reason: str | None
    response_body: str
    template_name: str | None
    quick_replies: list[str]
    # Interactive WhatsApp reply buttons (max 3). Each item is
    # {"id": str, "label": str, "action": str}. The gateway routes to the
    # interactive button send path when this list is non-empty AND the reply
    # is freeform (in-CSW). Outside CSW, buttons are silently dropped — Meta
    # would require a template-with-buttons send instead.
    buttons: list[dict[str, str]]
    # Interactive WhatsApp list rows (max 10). Each item is
    # {"id": str, "title": str, "description": str | None}. Mutually
    # exclusive with `buttons` at the wire level — gateway prefers list when
    # both present. Used by the booking agent to render slot picks instead
    # of an enumerated text body.
    list_rows: list[dict[str, str]]
    list_button_label: str | None
    list_section_title: str | None
    audit_reasons: list[str]
    ticket_id: str | None

    # Multi-turn memory + flow state (per-patient thread).
    messages: Annotated[list[Any], add_messages]
    current_flow: str | None       # None | "booking" | (future: "adherence" | ...)
    flow_state: dict[str, Any]     # sub-agent scratchpad (e.g. proposed slots)

    # Onboarding state machine. Stamped from patients.onboarding_step on
    # every turn so the supervisor can route to the onboarding handler
    # while the patient hasn't finished their initial setup.
    onboarding_step: str | None


@dataclass(frozen=True)
class WorkflowResult:
    intent: Intent
    risk_level: RiskLevel
    use_template: bool
    policy_reason: str
    policy_reason_codes: tuple[str, ...]
    flow_action: str
    escalation_required: bool
    escalation_reason: str | None
    response_body: str
    template_name: str | None
    quick_replies: list[str]
    audit_reasons: list[str]
    ticket_id: str | None = None


def _normalize_now(now: datetime | None) -> datetime:
    base = now or datetime.now(timezone.utc)
    return base if base.tzinfo else base.replace(tzinfo=timezone.utc)


def _normalize_last_user_message(last_user_message_at: datetime | None) -> datetime | None:
    if last_user_message_at is None:
        return None
    return (
        last_user_message_at.replace(tzinfo=timezone.utc)
        if last_user_message_at.tzinfo is None
        else last_user_message_at.astimezone(timezone.utc)
    )


def _detect_intent_rules(text: str | None) -> Intent:
    if not text:
        return "general_question"

    lower = text.lower()
    # Booking / appointment-management runs BEFORE the followup check
    # ("appointment" appears in both) because patient-initiated booking,
    # cancel, and reschedule should win when paired with the verbs below.
    if any(
        x in lower
        for x in [
            "book ",
            "book a",
            "book an",
            "schedule",
            "see the doctor",
            "want to see",
            "appointment with",
            "consult with",
            # cancel / reschedule against an appointment / booking
            "cancel my appointment",
            "cancel my booking",
            "cancel the appointment",
            "cancel appointment",
            "reschedule",
            "move my appointment",
            "move my booking",
            "change my appointment",
            "change my booking",
            "what appointments",
            "my appointments",
            "upcoming appointment",
            "do i have any appointment",
        ]
    ):
        return "booking_request"
    if any(x in lower for x in ["taken", "snooze", "skip", "missed", "forgot", "side effect", "out of stock", "confused", "cost"]):
        return "adherence_update"
    if any(x in lower for x in ["refill", "reorder", "run out", "update count"]):
        return "refill_request"
    if any(x in lower for x in ["booked", "completed", "reviewed", "lab", "follow-up", "followup"]):
        return "followup_update"
    if "appointment" in lower:
        # Generic "appointment" without a booking verb = followup status update.
        return "followup_update"
    if any(x in lower for x in ["symptom", "breath", "pain", "dizzy", "fever", "bleeding", "wheezing", "hypo", "high bp"]):
        return "symptom_report"
    if "pregnan" in lower or "trimester" in lower:
        return "pregnancy_checklist"
    return "general_question"


async def _detect_intent(text: str | None) -> Intent:
    if not text:
        return "general_question"
    llm_intent = await get_llm().classify_intent(text)
    if llm_intent is not None:
        return llm_intent
    return _detect_intent_rules(text)


_SEVERITY_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _risk_triage_rules(intent: Intent, text: str) -> tuple[RiskLevel, bool, str | None]:
    lower = text.lower()
    if any(x in lower for x in ["unconscious", "cannot breathe", "severe bleeding", "chest pain"]):
        return "critical", True, "critical_red_flag"
    if intent == "symptom_report" and any(x in lower for x in ["bleeding", "wheezing", "hypo", "very high bp", "breathless"]):
        return "high", True, "high_risk_symptom_report"
    if intent == "adherence_update" and any(x in lower for x in ["side effect", "confused"]):
        return "medium", True, "adherence_safety_check"
    return "low", False, None


async def _risk_triage(intent: Intent, text: str) -> tuple[RiskLevel, bool, str | None]:
    """Rule-based floor + LLM augmenter. The LLM may escalate but never reduce."""
    rule_severity, rule_escalation, rule_reason = _risk_triage_rules(intent, text)
    if not text:
        return rule_severity, rule_escalation, rule_reason

    augmented = await get_llm().augment_safety(
        text, intent=intent, current_severity=rule_severity
    )
    if augmented is None:
        return rule_severity, rule_escalation, rule_reason

    new_severity, new_reason = augmented
    if _SEVERITY_RANK[new_severity] <= _SEVERITY_RANK[rule_severity]:
        return rule_severity, rule_escalation, rule_reason

    escalation = new_severity in {"high", "critical"} or rule_escalation
    reason = (new_reason or rule_reason or f"llm_escalated_{new_severity}").strip() or rule_reason
    return new_severity, escalation, reason


def _compose_rules(intent: Intent, escalation_required: bool) -> str:
    if intent == "adherence_update":
        body = (
            "Adherence update received. If you missed a dose, reply FORGOT, SIDE_EFFECT, "
            "OUT_OF_STOCK, CONFUSED, COST, or OTHER."
        )
    elif intent == "refill_request":
        body = "Refill workflow started. Reply REORDER or UPDATE COUNT."
    elif intent == "followup_update":
        body = "Follow-up update received. Reply BOOKED, COMPLETED, or REVIEWED to track closure."
    elif intent == "pregnancy_checklist":
        body = "Pregnancy checklist support is ready. Reply HELP for clinic guidance."
    elif intent == "symptom_report":
        body = "Thanks for sharing symptoms. A clinician may need to review this."
    elif intent == "booking_request":
        # Hit when the booking_agent isn't reachable (LANGGRAPH_ENABLED=0 sync
        # fallback). Punt to manual scheduling so we don't pretend to book.
        body = (
            "Got your booking request — our booking agent is offline right now. "
            "We'll text you back shortly to schedule, or reply CALL for help now."
        )
    else:
        body = "Got it. Reply HELP for support or CALL for clinician callback."

    if escalation_required:
        body = f"{body} Reply CALL now for urgent support."
    return body


async def _compose(
    intent: Intent,
    escalation_required: bool,
    use_template: bool,
    text: str = "",
    preferred_language: str | None = None,
) -> tuple[str, str | None, list[str]]:
    quick_replies = ["CALL", "HELP"]
    template_name = "escalate_call_v1" if use_template else None

    body: str | None = None
    if text:
        body = await get_llm().compose_reply(
            intent=intent,
            text=text,
            escalation_required=escalation_required,
            use_template=use_template,
            preferred_language=preferred_language,
        )
    if not body:
        # Deterministic fallback is English-only by design — the LLM is
        # the multilingual surface. If the LLM is offline AND the
        # patient prefers a non-English language, English is still
        # safer than silence; ops can follow up via the doctor-reply
        # path if needed.
        body = _compose_rules(intent, escalation_required)

    return body, template_name, quick_replies


class _NoOpAuditTrail(AuditTrail):
    """Audit trail that drops decisions on the floor (used by the sync fallback)."""

    async def log_policy_decision(self, decision: PolicyDecision) -> None:  # type: ignore[override]
        return None


async def _evaluate_policy_decision(
    *,
    patient_id: str,
    intent: str,
    last_inbound: datetime | None,
    now: datetime,
    audit_trail: AuditTrail | None = None,
) -> PolicyDecision:
    state_store = PatientStateStore()
    if last_inbound is not None:
        await state_store.set_last_inbound_timestamp(patient_id, last_inbound)
    gate = PolicyGate(state_store, audit_trail or _NoOpAuditTrail())
    return await gate.evaluate(
        patient_id=patient_id,
        intent=intent,
        requested_flow="patient_inbound_response",
        now=now,
    )


async def run_agent_workflow(
    *,
    message_id: str,
    patient_id: str,
    text: str | None,
    phone: str | None,
    last_user_message_at: datetime | None,
    now: datetime | None = None,
    preferred_language: str | None = None,
) -> WorkflowResult:
    """Run the orchestrator agent workflow (async fallback runner)."""

    safe_now = _normalize_now(now)
    normalized_last = _normalize_last_user_message(last_user_message_at)
    inbound_text = (text or "").strip()

    intent = await _detect_intent(inbound_text)
    decision = await _evaluate_policy_decision(
        patient_id=patient_id,
        intent=intent,
        last_inbound=normalized_last,
        now=safe_now,
    )
    risk_level, escalation_required, escalation_reason = await _risk_triage(intent, inbound_text)

    body, template_name, quick_replies = await _compose(
        intent=intent,
        escalation_required=escalation_required,
        use_template=not decision.allow_freeform,
        text=inbound_text,
        preferred_language=preferred_language,
    )

    audit_reasons = list(decision.reason_codes)
    if escalation_reason:
        audit_reasons.append(escalation_reason)

    primary_reason = decision.reason_codes[0] if decision.reason_codes else ""

    return WorkflowResult(
        intent=intent,
        risk_level=risk_level,
        use_template=not decision.allow_freeform,
        policy_reason=primary_reason,
        policy_reason_codes=tuple(decision.reason_codes),
        flow_action=decision.flow_action,
        escalation_required=escalation_required,
        escalation_reason=escalation_reason,
        response_body=body,
        template_name=template_name,
        quick_replies=quick_replies,
        audit_reasons=audit_reasons,
    )


_SEVERITY_TO_PRIORITY: dict[str, tuple[str, int]] = {
    "critical": ("p0", 5),
    "high": ("p1", 15),
    "medium": ("p2", 60),
    "low": ("p3", 240),
}


async def _default_human_handoff(state: AgentState) -> dict[str, Any]:
    """Create an ops_ticket for an escalated case. Returns a state delta."""
    from app.db.repositories import ops_tickets as ops_tickets_repo
    from app.db.session import get_sessionmaker

    severity = state.get("risk_level", "low")
    priority, sla = _SEVERITY_TO_PRIORITY.get(severity, ("p3", 240))

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        ticket = await ops_tickets_repo.create(
            db,
            patient_id=state.get("patient_id", "unknown"),
            category="agent_escalation",
            priority=priority,
            sla_minutes=sla,
            notes=state.get("escalation_reason"),
        )
        await db.commit()
        return {"ticket_id": str(ticket.id)}


async def _ingest_node(state: AgentState) -> dict[str, Any]:
    """Normalize timestamps + text; record the inbound HumanMessage in state.messages."""
    text = state.get("text", "").strip()
    delta: dict[str, Any] = {
        "now_utc": _normalize_now(state.get("now_utc")),
        "last_user_message_at": _normalize_last_user_message(state.get("last_user_message_at")),
        "text": text,
    }
    if text:
        # Append-only via the add_messages reducer. We use a plain dict shape
        # so we don't force a langchain_core dependency on the rest of the app.
        delta["messages"] = [{"role": "user", "content": text}]
    return delta


async def _upsert_patient_node(state: AgentState) -> dict[str, Any]:
    """Ensure a patients row exists for this WhatsApp wa_id. Stash the FK +
    the onboarding_step + preferred_language so the next router can
    short-circuit when needed.

    Auto-detect language on the first inbound from a patient who's
    still on the default ``en`` preference. Once we've set a
    non-default language (here OR via the ops UI), this path stops
    overwriting — the value is sticky so a one-off English message
    from a Hindi-preferring patient doesn't flicker their preference.
    """
    from app import i18n
    from app.db.repositories import patients as patients_repo
    from app.db.session import get_sessionmaker
    from services.orchestrator.caregiver_handler import (
        looks_like_caregiver_action,
    )
    from services.orchestrator.dose_handler import looks_like_dose_action
    from services.orchestrator.lab_handler import looks_like_lab_action
    from services.orchestrator.prescription_handler import (
        looks_like_prescription_upload,
    )
    from services.orchestrator.recap_handler import looks_like_recap_action
    from services.orchestrator.refill_handler import looks_like_refill_action

    phone = state.get("phone") or state.get("patient_id", "")
    if not phone:
        return {"patient_db_id": None, "onboarding_step": None}

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        row = await patients_repo.upsert_by_phone(db, phone=phone)

        # First-inbound language auto-detect. Gates:
        #   - patient is still on the default ``en`` (sticky once set)
        #   - inbound text is non-trivial (≥ ~3 words / 12 chars)
        #   - inbound is NOT a structured action-tap marker (those are
        #     protocol tokens, not natural language)
        text = (state.get("text") or "").strip()
        is_action_tap = (
            looks_like_dose_action(text)
            or looks_like_refill_action(text)
            or looks_like_lab_action(text)
            or looks_like_recap_action(text)
            or looks_like_caregiver_action(text)
            or looks_like_prescription_upload(text)
        )
        if (
            row.preferred_language == i18n.DEFAULT_LANGUAGE_CODE
            and len(text) >= 12
            and not is_action_tap
        ):
            detection = await get_llm().detect_language(text)
            if detection is not None:
                code, confidence = detection
                # Only flip on HIGH confidence + a non-default,
                # supported code. Marginal guesses leave the patient
                # at English so an ops correction stays the easy path
                # for ambiguous cases.
                if (
                    confidence == "high"
                    and code != "unknown"
                    and i18n.is_supported(code)
                    and code != i18n.DEFAULT_LANGUAGE_CODE
                ):
                    row = await patients_repo.update_preferred_language(
                        db, row.id, preferred_language=code
                    )

        await db.commit()
        return {
            "patient_db_id": row.id,
            "onboarding_step": row.onboarding_step,
            "preferred_language": row.preferred_language or "en",
        }


async def _detect_intent_node(state: AgentState) -> AgentState:
    state["intent"] = await _detect_intent(state.get("text", ""))
    return state


async def _policy_node(state: AgentState) -> AgentState:
    """Run PolicyGate inside the graph, persisting the decision via DbAuditTrail."""
    from app.db.session import get_sessionmaker
    from services.orchestrator.db_policy_gate import DbAuditTrail

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        decision = await _evaluate_policy_decision(
            patient_id=state.get("patient_id", ""),
            intent=state.get("intent", "general_question"),
            last_inbound=state.get("last_user_message_at"),
            now=state["now_utc"],
            audit_trail=DbAuditTrail(db),
        )
        await db.commit()

    state["in_customer_service_window"] = decision.allow_freeform
    state["use_template"] = not decision.allow_freeform
    state["policy_reason_codes"] = list(decision.reason_codes)
    state["policy_reason"] = decision.reason_codes[0] if decision.reason_codes else ""
    state["flow_action"] = decision.flow_action
    return state


async def _safety_node(state: AgentState) -> AgentState:
    risk, escalate, reason = await _risk_triage(state["intent"], state.get("text", ""))
    state["risk_level"] = risk
    state["escalation_required"] = escalate
    state["escalation_reason"] = reason
    return state


async def _compose_node(state: AgentState) -> dict[str, Any]:
    body, template_name, replies = await _compose(
        intent=state["intent"],
        escalation_required=state.get("escalation_required", False),
        use_template=state.get("use_template", True),
        text=state.get("text", ""),
        preferred_language=state.get("preferred_language"),
    )
    reasons = list(state.get("policy_reason_codes", []))
    if state.get("escalation_reason"):
        reasons.append(state["escalation_reason"])
    return {
        "response_body": body,
        "template_name": template_name,
        "quick_replies": replies,
        "audit_reasons": reasons,
        "messages": [{"role": "assistant", "content": body}],
    }


def _route_after_safety(
    state: AgentState,
) -> Literal["human_handoff", "booking_agent", "compose"]:
    """Supervisor routing.

    Priority order:
        1. Escalation always wins (safety floor).
        2. An active flow keeps routing to the same sub-agent (multi-turn
           continuity — patient saying "1" mid-booking lands back in
           booking_agent regardless of what intent the LLM thinks it is).
        3. Else the detected intent picks a sub-agent.
        4. Default: compose (the existing canned/LLM reply path).
    """
    if state.get("escalation_required"):
        return "human_handoff"
    if state.get("current_flow") == "booking":
        return "booking_agent"
    if state.get("intent") == "booking_request":
        return "booking_agent"
    return "compose"


async def _prescription_handler_node(state: AgentState) -> dict[str, Any]:
    """Inbound prescription image: short-circuit, create the row, run the
    vision LLM, and reply with a friendly ack. Skips intent/safety/LLM."""
    from services.orchestrator.prescription_handler import (
        handle_prescription_upload,
    )

    delta = await handle_prescription_upload(
        patient_phone=state.get("patient_id", ""),
        new_user_text=state.get("text", ""),
    )
    if delta is None:
        return {"audit_reasons": ["prescription_unparsed"]}
    delta.setdefault(
        "messages", [{"role": "assistant", "content": delta["response_body"]}]
    )
    delta.setdefault("flow_action", "ALLOW")
    return delta


async def _side_effect_handler_node(state: AgentState) -> dict[str, Any]:
    """Inbound side-effect / adverse-reaction report. Opens a high-
    priority ops ticket with the patient's active regimens captured
    in the notes, sends an immediate ack with emergency-services
    guidance. Skips intent / safety / LLM."""
    from services.orchestrator.side_effect_handler import (
        handle_side_effect_report,
    )

    delta = await handle_side_effect_report(
        patient_phone=state.get("patient_id", ""),
        new_user_text=state.get("text", ""),
    )
    if delta is None:
        # Defensive — handler returns None only when the patient
        # row itself is missing AFTER ack.
        return {"audit_reasons": ["side_effect_no_patient"]}
    delta.setdefault(
        "messages", [{"role": "assistant", "content": delta["response_body"]}]
    )
    delta.setdefault("flow_action", "ALLOW")
    return delta


async def _lookup_handler_node(state: AgentState) -> dict[str, Any]:
    """Self-service lookup — patient asks "what meds am I on" or
    "what labs do I have"; we query the DB and render a structured
    response without going through the LLM compose path. Skips
    intent / safety / LLM."""
    from services.orchestrator.lookup_handler import (
        classify_lookup_query,
        handle_lookup_query,
    )

    text = state.get("text", "")
    query_type = classify_lookup_query(text)
    if query_type is None:
        # Defensive: router should only have sent us here when the
        # classifier matched, but never crash on a stale state.
        return {"audit_reasons": ["lookup_unclassified"]}

    delta = await handle_lookup_query(
        patient_phone=state.get("patient_id", ""),
        query_type=query_type,
        new_user_text=text,
    )
    if delta is None:
        return {"audit_reasons": ["lookup_no_data"]}
    delta.setdefault(
        "messages", [{"role": "assistant", "content": delta["response_body"]}]
    )
    delta.setdefault("flow_action", "ALLOW")
    return delta


async def _optout_handler_node(state: AgentState) -> dict[str, Any]:
    """STOP / START keyword inbound. Routes to handle_optout when the
    inbound looks like opt-out, handle_optin otherwise (the router
    only sends us here when one of those matchers fires)."""
    from services.orchestrator.optout_handler import (
        handle_optin,
        handle_optout,
        looks_like_optout,
    )

    text = state.get("text", "")
    runner = handle_optout if looks_like_optout(text) else handle_optin
    delta = await runner(
        patient_phone=state.get("patient_id", ""),
        new_user_text=text,
    )
    if delta is None:
        # Patient row missing — defensive fall-through. The compose
        # node won't have anything specific to say, but the rest of
        # the pipeline still runs.
        return {"audit_reasons": ["optout_no_patient"]}
    delta.setdefault(
        "messages", [{"role": "assistant", "content": delta["response_body"]}]
    )
    delta.setdefault("flow_action", "ALLOW")
    return delta


async def _onboarding_handler_node(state: AgentState) -> dict[str, Any]:
    """One turn of the onboarding state machine — name → cohorts →
    consent → done. Skips intent / safety / LLM."""
    from services.orchestrator.onboarding_handler import handle_onboarding

    delta = await handle_onboarding(
        patient_phone=state.get("patient_id", ""),
        new_user_text=state.get("text", ""),
    )
    if delta is None:
        # State already DONE — caller should have routed elsewhere.
        return {"audit_reasons": ["onboarding_no_op"]}
    delta.setdefault(
        "messages", [{"role": "assistant", "content": delta["response_body"]}]
    )
    delta.setdefault("flow_action", "ALLOW")
    return delta


def _route_for_onboarding(
    state: AgentState,
) -> Literal[
    "optout_handler",
    "onboarding_handler",
    "prescription_handler",
    "dose_handler",
    "refill_handler",
    "lab_handler",
    "recap_handler",
    "caregiver_handler",
    "side_effect_handler",
    "lookup_handler",
    "detect_intent",
]:
    """Top-level pre-LLM router. STOP / START have HIGHEST priority —
    a patient saying STOP must opt out regardless of onboarding state
    or what action-tap matchers might otherwise claim them.

    After opt-in/opt-out, onboarding takes over: while the patient
    hasn't completed profile setup, every inbound is consumed by
    the onboarding handler regardless of content. Once onboarding
    is done, structured taps + uploads short-circuit to their
    handlers; self-service lookup queries ("what meds am I on")
    short-circuit before detect_intent so the LLM compose path
    never has to fabricate medication lists. Anything else flows
    on to detect_intent."""
    from services.orchestrator.dose_handler import looks_like_dose_action
    from services.orchestrator.lab_handler import looks_like_lab_action
    from services.orchestrator.lookup_handler import classify_lookup_query
    from services.orchestrator.onboarding_handler import is_onboarding_active
    from services.orchestrator.optout_handler import (
        looks_like_optin,
        looks_like_optout,
    )
    from services.orchestrator.prescription_handler import (
        looks_like_prescription_upload,
    )
    from services.orchestrator.caregiver_handler import (
        looks_like_caregiver_action,
    )
    from services.orchestrator.recap_handler import looks_like_recap_action
    from services.orchestrator.refill_handler import looks_like_refill_action
    from services.orchestrator.side_effect_handler import (
        looks_like_side_effect_report,
    )

    text = state.get("text", "")

    # STOP / START win unconditionally. Honouring opt-out is a
    # compliance requirement; we'd rather risk routing a "stop being
    # silly" false-positive (the matcher anchors prevent this) than
    # let a real STOP get swallowed by onboarding or an action-tap.
    if looks_like_optout(text) or looks_like_optin(text):
        return "optout_handler"

    if is_onboarding_active(state.get("onboarding_step")):
        return "onboarding_handler"

    if looks_like_prescription_upload(text):
        return "prescription_handler"
    if looks_like_dose_action(text):
        return "dose_handler"
    if looks_like_refill_action(text):
        return "refill_handler"
    if looks_like_lab_action(text):
        return "lab_handler"
    if looks_like_recap_action(text):
        return "recap_handler"
    # Caregiver consent comes AFTER recap because plain "OK" / "QUESTION"
    # routes to recap_handler; the caregiver matchers are tighter
    # ("YES" / "NO" / "decline") and don't overlap with recap copy.
    if looks_like_caregiver_action(text):
        return "caregiver_handler"
    # Side-effect / adverse-reaction reports beat lookup queries —
    # a patient saying "the meds are making me dizzy, what am I
    # taking?" is reporting a clinical event first, asking a query
    # second. Patient-safety wins.
    if looks_like_side_effect_report(text):
        return "side_effect_handler"
    # Self-service lookup queries — strict-anchored classifier so
    # generic mentions of meds/labs ("I forgot to take my meds")
    # don't accidentally route here. Misses fall through to the LLM.
    if classify_lookup_query(text) is not None:
        return "lookup_handler"
    return "detect_intent"


async def _dose_handler_node(state: AgentState) -> dict[str, Any]:
    """Deterministic CRUD against an AdherenceEvent in response to a
    Taken / Snoozed / Skipped button tap. Skips intent + safety + LLM."""
    from services.orchestrator.dose_handler import handle_dose_action

    delta = await handle_dose_action(
        patient_phone=state.get("patient_id", ""),
        new_user_text=state.get("text", ""),
    )
    if delta is None:
        # Pre-check passed but inbound didn't actually parse — fall through
        # by returning a minimal state delta the rest of the graph can
        # interpret. The compose node will still produce a generic reply.
        return {"audit_reasons": ["dose_action_unparsed"]}

    delta.setdefault("messages", [{"role": "assistant", "content": delta["response_body"]}])
    delta.setdefault("flow_action", "ALLOW")
    return delta


async def _refill_handler_node(state: AgentState) -> dict[str, Any]:
    """Same shape as _dose_handler_node but for refill button taps —
    Refilled / Snooze 1 day / Need help. Resets supply_started_on,
    re-enqueues a snooze reminder, or opens an ops_ticket."""
    from services.orchestrator.refill_handler import handle_refill_action

    delta = await handle_refill_action(
        patient_phone=state.get("patient_id", ""),
        new_user_text=state.get("text", ""),
    )
    if delta is None:
        return {"audit_reasons": ["refill_action_unparsed"]}
    delta.setdefault(
        "messages", [{"role": "assistant", "content": delta["response_body"]}]
    )
    delta.setdefault("flow_action", "ALLOW")
    return delta


async def _lab_handler_node(state: AgentState) -> dict[str, Any]:
    """Lab follow-up button taps — Booked / Completed / Need help. Pure
    state-machine CRUD against ``lab_followups``."""
    from services.orchestrator.lab_handler import handle_lab_action

    delta = await handle_lab_action(
        patient_phone=state.get("patient_id", ""),
        new_user_text=state.get("text", ""),
    )
    if delta is None:
        return {"audit_reasons": ["lab_action_unparsed"]}
    delta.setdefault(
        "messages", [{"role": "assistant", "content": delta["response_body"]}]
    )
    delta.setdefault("flow_action", "ALLOW")
    return delta


async def _recap_handler_node(state: AgentState) -> dict[str, Any]:
    """After-visit recap quick-reply taps — Got it (ack) / Question.
    Pure state-machine CRUD against ``appointment_recaps``; opens an
    ops_ticket for the question path."""
    from services.orchestrator.recap_handler import handle_recap_action

    delta = await handle_recap_action(
        patient_phone=state.get("patient_id", ""),
        new_user_text=state.get("text", ""),
    )
    if delta is None:
        # No recent recap — let the inbound flow on to detect_intent so
        # a generic "OK" doesn't get swallowed when there's nothing to ack.
        return {"audit_reasons": ["recap_action_no_recent_recap"]}
    delta.setdefault(
        "messages", [{"role": "assistant", "content": delta["response_body"]}]
    )
    delta.setdefault("flow_action", "ALLOW")
    return delta


async def _caregiver_handler_node(state: AgentState) -> dict[str, Any]:
    """Caregiver consent reply (YES / NO / marker tap). Resolves the
    pending caregiver row by sender phone, flips consent_status, and
    sends a deterministic confirmation back. Falls through with an
    audit-only delta if the inbound looked caregiver-shaped but no
    pending row matched (so we don't swallow a stray "yes")."""
    from services.orchestrator.caregiver_handler import handle_caregiver_action

    delta = await handle_caregiver_action(
        sender_phone=state.get("patient_id", ""),
        new_user_text=state.get("text", ""),
    )
    if delta is None:
        return {"audit_reasons": ["caregiver_action_no_pending"]}
    delta.setdefault(
        "messages", [{"role": "assistant", "content": delta["response_body"]}]
    )
    delta.setdefault("flow_action", "ALLOW")
    return delta


# NOTE: _route_for_button_action was merged into _route_for_onboarding
# above so onboarding can gate ALL routing in one place.


async def _booking_agent_node(state: AgentState) -> dict[str, Any]:
    """Run one turn of the booking ReAct loop.

    Delegates to :mod:`services.orchestrator.booking_agent`, then merges its
    delta with the audit_reasons + template defaults the rest of the graph
    expects (compose node doesn't run after this — booking owns its reply).
    """
    from services.orchestrator.booking_agent import run_booking_agent

    delta = await run_booking_agent(
        patient_phone=state.get("patient_id", ""),
        patient_db_id=state.get("patient_db_id"),
        state_messages=state.get("messages") or [],
        new_user_text=state.get("text", ""),
        flow_state=state.get("flow_state"),
        preferred_language=state.get("preferred_language"),
    )

    audit = list(state.get("policy_reason_codes", []))
    audit.append(
        "booking_agent_completed"
        if delta.get("current_flow") is None
        else "booking_agent_in_progress"
    )
    delta["audit_reasons"] = audit
    delta.setdefault("template_name", None)
    delta.setdefault("quick_replies", ["CALL", "HELP"])
    delta.setdefault("buttons", [])
    delta.setdefault("list_rows", [])
    delta.setdefault("list_button_label", None)
    delta.setdefault("list_section_title", None)
    return delta


def build_langgraph_workflow(
    checkpointer: Any | None = None,
    *,
    human_handoff: Callable[[AgentState], Awaitable[Any]] | None = None,
) -> Any | None:
    """Build the compiled LangGraph workflow when langgraph is installed.

    All node functions are async, so the compiled graph must be invoked with
    ``await graph.ainvoke(...)``. Returns ``None`` when ``langgraph`` is not
    installed so callers can fall back to :func:`run_agent_workflow`.
    """

    try:
        from langgraph.graph import END, START, StateGraph
    except Exception:
        return None

    handoff_fn = human_handoff or _default_human_handoff

    graph: Any = StateGraph(AgentState)
    graph.add_node("ingest", _ingest_node)
    graph.add_node("upsert_patient", _upsert_patient_node)
    graph.add_node("optout_handler", _optout_handler_node)
    graph.add_node("lookup_handler", _lookup_handler_node)
    graph.add_node("side_effect_handler", _side_effect_handler_node)
    graph.add_node("onboarding_handler", _onboarding_handler_node)
    graph.add_node("prescription_handler", _prescription_handler_node)
    graph.add_node("dose_handler", _dose_handler_node)
    graph.add_node("refill_handler", _refill_handler_node)
    graph.add_node("lab_handler", _lab_handler_node)
    graph.add_node("recap_handler", _recap_handler_node)
    graph.add_node("caregiver_handler", _caregiver_handler_node)
    graph.add_node("detect_intent", _detect_intent_node)
    graph.add_node("policy", _policy_node)
    graph.add_node("safety", _safety_node)
    graph.add_node("human_handoff", handoff_fn)
    graph.add_node("booking_agent", _booking_agent_node)
    graph.add_node("compose", _compose_node)

    graph.add_edge(START, "ingest")
    graph.add_edge("ingest", "upsert_patient")
    # Top-level pre-LLM router. Onboarding gates everything; once onboarded,
    # structured uploads + button taps short-circuit; else fall through to
    # detect_intent → policy → safety → supervisor.
    graph.add_conditional_edges(
        "upsert_patient",
        _route_for_onboarding,
        {
            "optout_handler": "optout_handler",
            "onboarding_handler": "onboarding_handler",
            "prescription_handler": "prescription_handler",
            "dose_handler": "dose_handler",
            "refill_handler": "refill_handler",
            "lab_handler": "lab_handler",
            "recap_handler": "recap_handler",
            "caregiver_handler": "caregiver_handler",
            "side_effect_handler": "side_effect_handler",
            "lookup_handler": "lookup_handler",
            "detect_intent": "detect_intent",
        },
    )
    graph.add_edge("detect_intent", "policy")
    graph.add_edge("policy", "safety")
    graph.add_conditional_edges(
        "safety",
        _route_after_safety,
        {
            "human_handoff": "human_handoff",
            "booking_agent": "booking_agent",
            "compose": "compose",
        },
    )
    graph.add_edge("human_handoff", "compose")
    graph.add_edge("booking_agent", END)
    graph.add_edge("optout_handler", END)
    graph.add_edge("lookup_handler", END)
    graph.add_edge("side_effect_handler", END)
    graph.add_edge("onboarding_handler", END)
    graph.add_edge("prescription_handler", END)
    graph.add_edge("dose_handler", END)
    graph.add_edge("refill_handler", END)
    graph.add_edge("lab_handler", END)
    graph.add_edge("recap_handler", END)
    graph.add_edge("caregiver_handler", END)
    graph.add_edge("compose", END)

    return graph.compile(checkpointer=checkpointer)

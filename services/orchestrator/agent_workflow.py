from __future__ import annotations

"""LangGraph-aligned agent workflow for Medagent orchestrator.

This module follows current agent best practices:
- strongly-typed shared state
- deterministic routing for safety/policy decisions
- explicit human escalation branch
- graph compilation with optional checkpointer support
- pure-Python fallback runner when LangGraph is unavailable
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, TypedDict


Intent = Literal[
    "adherence_update",
    "refill_request",
    "symptom_report",
    "pregnancy_checklist",
    "followup_update",
    "general_question",
]
RiskLevel = Literal["low", "medium", "high", "critical"]


class AgentState(TypedDict, total=False):
    message_id: str
    patient_id: str
    phone: str | None
    text: str
    now_utc: datetime
    last_user_message_at: datetime | None
    in_customer_service_window: bool
    use_template: bool
    policy_reason: str
    intent: Intent
    risk_level: RiskLevel
    escalation_required: bool
    escalation_reason: str | None
    response_body: str
    template_name: str | None
    quick_replies: list[str]
    audit_reasons: list[str]


@dataclass(frozen=True)
class WorkflowResult:
    intent: Intent
    risk_level: RiskLevel
    use_template: bool
    policy_reason: str
    escalation_required: bool
    escalation_reason: str | None
    response_body: str
    template_name: str | None
    quick_replies: list[str]
    audit_reasons: list[str]


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


def _detect_intent(text: str | None) -> Intent:
    if not text:
        return "general_question"

    lower = text.lower()
    if any(x in lower for x in ["taken", "snooze", "skip", "missed", "forgot", "side effect", "out of stock", "confused", "cost"]):
        return "adherence_update"
    if any(x in lower for x in ["refill", "reorder", "run out", "update count"]):
        return "refill_request"
    if any(x in lower for x in ["booked", "completed", "reviewed", "lab", "appointment", "follow-up", "followup"]):
        return "followup_update"
    if any(x in lower for x in ["symptom", "breath", "pain", "dizzy", "fever", "bleeding", "wheezing", "hypo", "high bp"]):
        return "symptom_report"
    if "pregnan" in lower or "trimester" in lower:
        return "pregnancy_checklist"
    return "general_question"


def _policy_gate(now: datetime, last_user_message_at: datetime | None) -> tuple[bool, bool, str]:
    if last_user_message_at is None:
        return False, True, "No prior inbound message timestamp; require template send"

    in_window = (now - last_user_message_at) <= timedelta(hours=24)
    reason = "Within 24h freeform allowed" if in_window else "Outside 24h template required"
    return in_window, (not in_window), reason


def _risk_triage(intent: Intent, text: str) -> tuple[RiskLevel, bool, str | None]:
    lower = text.lower()
    if any(x in lower for x in ["unconscious", "cannot breathe", "severe bleeding", "chest pain"]):
        return "critical", True, "critical_red_flag"
    if intent == "symptom_report" and any(x in lower for x in ["bleeding", "wheezing", "hypo", "very high bp", "breathless"]):
        return "high", True, "high_risk_symptom_report"
    if intent == "adherence_update" and any(x in lower for x in ["side effect", "confused"]):
        return "medium", True, "adherence_safety_check"
    return "low", False, None


def _compose(intent: Intent, escalation_required: bool, use_template: bool) -> tuple[str, str | None, list[str]]:
    quick_replies = ["CALL", "HELP"]

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
    else:
        body = "Got it. Reply HELP for support or CALL for clinician callback."

    if escalation_required:
        body = f"{body} Reply CALL now for urgent support."

    return body, ("escalate_call_v1" if use_template else None), quick_replies


def run_agent_workflow(
    *,
    message_id: str,
    patient_id: str,
    text: str | None,
    phone: str | None,
    last_user_message_at: datetime | None,
    now: datetime | None = None,
) -> WorkflowResult:
    """Run the orchestrator agent workflow.

    This is the deterministic fallback runner and mirrors graph node transitions.
    """

    safe_now = _normalize_now(now)
    normalized_last = _normalize_last_user_message(last_user_message_at)
    inbound_text = (text or "").strip()

    intent = _detect_intent(inbound_text)
    in_window, use_template, policy_reason = _policy_gate(safe_now, normalized_last)
    risk_level, escalation_required, escalation_reason = _risk_triage(intent, inbound_text)

    body, template_name, quick_replies = _compose(
        intent=intent,
        escalation_required=escalation_required,
        use_template=use_template,
    )

    audit_reasons = [policy_reason]
    if escalation_reason:
        audit_reasons.append(escalation_reason)
    if not in_window:
        audit_reasons.append("template_required")

    return WorkflowResult(
        intent=intent,
        risk_level=risk_level,
        use_template=use_template,
        policy_reason=policy_reason,
        escalation_required=escalation_required,
        escalation_reason=escalation_reason,
        response_body=body,
        template_name=template_name,
        quick_replies=quick_replies,
        audit_reasons=audit_reasons,
    )


def build_langgraph_workflow(checkpointer: Any | None = None) -> Any | None:
    """Build a compiled LangGraph workflow when langgraph is installed.

    Returns None when langgraph is unavailable so callers can safely use fallback mode.
    """

    try:
        from langgraph.graph import END, START, StateGraph
    except Exception:
        return None

    graph = StateGraph(AgentState)

    def ingest(state: AgentState) -> AgentState:
        state["now_utc"] = _normalize_now(state.get("now_utc"))
        state["last_user_message_at"] = _normalize_last_user_message(state.get("last_user_message_at"))
        state["text"] = state.get("text", "").strip()
        return state

    def detect_intent_node(state: AgentState) -> AgentState:
        state["intent"] = _detect_intent(state.get("text", ""))
        return state

    def policy_node(state: AgentState) -> AgentState:
        in_window, use_template, reason = _policy_gate(
            state["now_utc"],
            state.get("last_user_message_at"),
        )
        state["in_customer_service_window"] = in_window
        state["use_template"] = use_template
        state["policy_reason"] = reason
        return state

    def safety_node(state: AgentState) -> AgentState:
        risk, escalate, reason = _risk_triage(state["intent"], state.get("text", ""))
        state["risk_level"] = risk
        state["escalation_required"] = escalate
        state["escalation_reason"] = reason
        return state

    def compose_node(state: AgentState) -> AgentState:
        body, template_name, replies = _compose(
            intent=state["intent"],
            escalation_required=state.get("escalation_required", False),
            use_template=state.get("use_template", True),
        )
        state["response_body"] = body
        state["template_name"] = template_name
        state["quick_replies"] = replies
        reasons = [state.get("policy_reason", "policy_unset")]
        if state.get("escalation_reason"):
            reasons.append(state["escalation_reason"])
        if state.get("use_template"):
            reasons.append("template_required")
        state["audit_reasons"] = reasons
        return state

    graph.add_node("ingest", ingest)
    graph.add_node("detect_intent", detect_intent_node)
    graph.add_node("policy", policy_node)
    graph.add_node("safety", safety_node)
    graph.add_node("compose", compose_node)

    graph.add_edge(START, "ingest")
    graph.add_edge("ingest", "detect_intent")
    graph.add_edge("detect_intent", "policy")
    graph.add_edge("policy", "safety")
    graph.add_edge("safety", "compose")
    graph.add_edge("compose", END)

    return graph.compile(checkpointer=checkpointer)

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional


FREEFORM_WINDOW_HOURS = 24
ESCALATION_ACTIONS = ["CALL", "talk to pharmacist", "talk to doctor"]
ALLOWED_FLOW_ACTIONS = {"ALLOW", "REROUTE", "REJECT"}
ALLOWED_OUTBOUND_MODES = {"FREEFORM", "TEMPLATE"}
DISALLOWED_MEDICINE_FLOWS = {
    "order_controlled_medicine",
    "sell_prescription_without_verification",
}
REGULATED_CONTENT_INTENTS = {
    "medicine_ordering",
    "controlled_substance_request",
}


class ReasonCode:
    FREEFORM_ALLOWED_WITHIN_WINDOW = "FREEFORM_ALLOWED_WITHIN_WINDOW"
    TEMPLATE_REQUIRED_OUTSIDE_WINDOW = "TEMPLATE_REQUIRED_OUTSIDE_WINDOW"
    TEMPLATE_REQUIRED_NO_INBOUND_FOUND = "TEMPLATE_REQUIRED_NO_INBOUND_FOUND"
    DISALLOWED_MEDICINE_ORDERING_FLOW = "DISALLOWED_MEDICINE_ORDERING_FLOW"
    REGULATED_CONTENT_REROUTED = "REGULATED_CONTENT_REROUTED"
    HUMAN_ESCALATION_EXPOSED = "HUMAN_ESCALATION_EXPOSED"


@dataclass
class PolicyDecision:
    patient_id: str
    allow_freeform: bool
    outbound_mode: str
    flow_action: str
    escalation_actions: List[str]
    reason_codes: List[str]
    details: Dict[str, str] = field(default_factory=dict)


class PatientStateStore:
    """In-memory store abstraction for last inbound timestamps."""

    def __init__(self) -> None:
        self._last_inbound: Dict[str, datetime] = {}

    def set_last_inbound_timestamp(self, patient_id: str, inbound_timestamp: datetime) -> None:
        if inbound_timestamp.tzinfo is None:
            inbound_timestamp = inbound_timestamp.replace(tzinfo=timezone.utc)
        self._last_inbound[patient_id] = inbound_timestamp.astimezone(timezone.utc)

    def get_last_inbound_timestamp(self, patient_id: str) -> Optional[datetime]:
        return self._last_inbound.get(patient_id)


class AuditTrail:
    def __init__(self) -> None:
        self.records: List[Dict[str, object]] = []

    def log_policy_decision(self, decision: PolicyDecision) -> None:
        if decision.flow_action not in ALLOWED_FLOW_ACTIONS:
            raise ValueError(f"Invalid flow action: {decision.flow_action}")
        if decision.outbound_mode not in ALLOWED_OUTBOUND_MODES:
            raise ValueError(f"Invalid outbound mode: {decision.outbound_mode}")

        self.records.append(
            {
                "type": "policy_decision",
                "patient_id": decision.patient_id,
                "outbound_mode": decision.outbound_mode,
                "flow_action": decision.flow_action,
                "reason_codes": decision.reason_codes,
                "details": decision.details,
                "logged_at": datetime.now(timezone.utc).isoformat(),
            }
        )


class PolicyGate:
    """Policy node for orchestrator message-routing decisions."""

    def __init__(self, state_store: PatientStateStore, audit_trail: AuditTrail) -> None:
        self.state_store = state_store
        self.audit_trail = audit_trail

    def evaluate(
        self,
        patient_id: str,
        intent: str,
        requested_flow: str,
        now: Optional[datetime] = None,
    ) -> PolicyDecision:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        else:
            now = now.astimezone(timezone.utc)

        reason_codes: List[str] = []
        details: Dict[str, str] = {
            "intent": intent,
            "requested_flow": requested_flow,
        }

        last_inbound = self.state_store.get_last_inbound_timestamp(patient_id)
        allow_freeform = False

        if last_inbound is None:
            reason_codes.append(ReasonCode.TEMPLATE_REQUIRED_NO_INBOUND_FOUND)
            details["last_inbound"] = "missing"
        else:
            elapsed = now - last_inbound
            if elapsed.total_seconds() < 0:
                elapsed = timedelta(0)
            details["last_inbound"] = last_inbound.isoformat()
            details["elapsed_since_last_inbound_seconds"] = str(int(elapsed.total_seconds()))
            if elapsed <= timedelta(hours=FREEFORM_WINDOW_HOURS):
                allow_freeform = True
                reason_codes.append(ReasonCode.FREEFORM_ALLOWED_WITHIN_WINDOW)
            else:
                reason_codes.append(ReasonCode.TEMPLATE_REQUIRED_OUTSIDE_WINDOW)

        flow_action = "ALLOW"
        if requested_flow in DISALLOWED_MEDICINE_FLOWS:
            flow_action = "REJECT"
            allow_freeform = False
            reason_codes.append(ReasonCode.DISALLOWED_MEDICINE_ORDERING_FLOW)
        elif intent in REGULATED_CONTENT_INTENTS:
            flow_action = "REROUTE"
            allow_freeform = False
            reason_codes.append(ReasonCode.REGULATED_CONTENT_REROUTED)

        reason_codes.append(ReasonCode.HUMAN_ESCALATION_EXPOSED)

        reason_codes = list(dict.fromkeys(reason_codes))

        decision = PolicyDecision(
            patient_id=patient_id,
            allow_freeform=allow_freeform,
            outbound_mode="FREEFORM" if allow_freeform else "TEMPLATE",
            flow_action=flow_action,
            escalation_actions=list(ESCALATION_ACTIONS),
            reason_codes=reason_codes,
            details=details,
        )
        self.audit_trail.log_policy_decision(decision)
        return decision

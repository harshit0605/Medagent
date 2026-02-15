from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

from services.orchestrator.policy_gate import AuditTrail, PolicyDecision, ReasonCode


@dataclass
class DeliveryResult:
    mode: str
    payload: Dict[str, object]


class FreeformSendAPI:
    def send(self, patient_id: str, text: str) -> DeliveryResult:
        return DeliveryResult(mode="FREEFORM", payload={"patient_id": patient_id, "text": text})


class TemplateSendAPI:
    def send(self, patient_id: str, template_name: str, variables: Dict[str, str]) -> DeliveryResult:
        return DeliveryResult(
            mode="TEMPLATE",
            payload={
                "patient_id": patient_id,
                "template_name": template_name,
                "variables": variables,
            },
        )


class WhatsAppGateway:
    def __init__(
        self,
        freeform_api: FreeformSendAPI,
        template_api: TemplateSendAPI,
        audit_trail: AuditTrail,
    ) -> None:
        self.freeform_api = freeform_api
        self.template_api = template_api
        self.audit_trail = audit_trail

    def send_outbound(
        self,
        patient_id: str,
        text: str,
        policy_decision: PolicyDecision,
        template_name: str = "patient_follow_up",
        template_variables: Optional[Dict[str, str]] = None,
    ) -> DeliveryResult:
        template_variables = template_variables or {}

        if policy_decision.outbound_mode == "FREEFORM":
            result = self.freeform_api.send(patient_id=patient_id, text=text)
            self._log_gateway_decision(patient_id, result.mode, policy_decision.reason_codes)
            return result

        template_variables = {**template_variables, "body": text}
        result = self.template_api.send(
            patient_id=patient_id,
            template_name=template_name,
            variables=template_variables,
        )
        reason_codes = list(policy_decision.reason_codes)
        if ReasonCode.TEMPLATE_REQUIRED_OUTSIDE_WINDOW not in reason_codes and ReasonCode.TEMPLATE_REQUIRED_NO_INBOUND_FOUND not in reason_codes:
            reason_codes.append(ReasonCode.TEMPLATE_REQUIRED_OUTSIDE_WINDOW)

        self._log_gateway_decision(patient_id, result.mode, reason_codes)
        return result

    def _log_gateway_decision(self, patient_id: str, mode: str, reason_codes: List[str]) -> None:
        self.audit_trail.records.append(
            {
                "type": "whatsapp_outbound_policy",
                "patient_id": patient_id,
                "mode": mode,
                "reason_codes": reason_codes,
                "logged_at": datetime.now(timezone.utc).isoformat(),
            }
        )

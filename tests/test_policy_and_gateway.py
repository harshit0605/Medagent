from datetime import datetime, timedelta, timezone
import unittest

from services.orchestrator.policy_gate import (
    AuditTrail,
    PatientStateStore,
    PolicyGate,
    ReasonCode,
)
from services.whatsapp_gateway.outbound import FreeformSendAPI, TemplateSendAPI, WhatsAppGateway


class PolicyGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = PatientStateStore()
        self.audit = AuditTrail()
        self.gate = PolicyGate(self.store, self.audit)
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def test_allows_freeform_within_24h_window(self) -> None:
        self.store.set_last_inbound_timestamp("p1", self.now - timedelta(hours=2))
        decision = self.gate.evaluate("p1", intent="general_question", requested_flow="support", now=self.now)
        self.assertTrue(decision.allow_freeform)
        self.assertEqual("FREEFORM", decision.outbound_mode)
        self.assertIn(ReasonCode.FREEFORM_ALLOWED_WITHIN_WINDOW, decision.reason_codes)

    def test_requires_template_outside_window(self) -> None:
        self.store.set_last_inbound_timestamp("p1", self.now - timedelta(hours=26))
        decision = self.gate.evaluate("p1", intent="general_question", requested_flow="support", now=self.now)
        self.assertFalse(decision.allow_freeform)
        self.assertEqual("TEMPLATE", decision.outbound_mode)
        self.assertIn(ReasonCode.TEMPLATE_REQUIRED_OUTSIDE_WINDOW, decision.reason_codes)

    def test_rejects_disallowed_medicine_flow_and_exposes_escalation(self) -> None:
        self.store.set_last_inbound_timestamp("p1", self.now - timedelta(hours=2))
        decision = self.gate.evaluate(
            "p1",
            intent="medicine_ordering",
            requested_flow="order_controlled_medicine",
            now=self.now,
        )
        self.assertEqual("REJECT", decision.flow_action)
        self.assertIn(ReasonCode.DISALLOWED_MEDICINE_ORDERING_FLOW, decision.reason_codes)
        self.assertIn("CALL", decision.escalation_actions)


    def test_policy_gate_handles_naive_now_and_dedupes_reason_codes(self) -> None:
        naive_now = datetime(2026, 1, 1)
        self.store.set_last_inbound_timestamp("p3", datetime(2025, 12, 31, 23, 0, 0))

        decision = self.gate.evaluate("p3", intent="general_question", requested_flow="support", now=naive_now)

        self.assertIn(decision.outbound_mode, ["FREEFORM", "TEMPLATE"])
        self.assertEqual(len(decision.reason_codes), len(set(decision.reason_codes)))


class AuditTrailValidationTests(unittest.TestCase):
    def test_rejects_invalid_policy_shapes(self) -> None:
        audit = AuditTrail()

        with self.assertRaises(ValueError):
            audit.log_policy_decision(
                type("D", (), {
                    "patient_id": "p1",
                    "outbound_mode": "BAD",
                    "flow_action": "ALLOW",
                    "reason_codes": [],
                    "details": {},
                })()
            )


class WhatsAppGatewayTests(unittest.TestCase):
    def test_enforces_template_send_when_policy_requires_template(self) -> None:
        store = PatientStateStore()
        audit = AuditTrail()
        gate = PolicyGate(store, audit)
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        store.set_last_inbound_timestamp("p2", now - timedelta(hours=48))
        decision = gate.evaluate("p2", intent="general_question", requested_flow="support", now=now)

        gateway = WhatsAppGateway(FreeformSendAPI(), TemplateSendAPI(), audit)
        result = gateway.send_outbound(
            "p2",
            text="Your order is ready",
            policy_decision=decision,
            template_name="order_update",
            template_variables={"order_id": "123"},
        )

        self.assertEqual("TEMPLATE", result.mode)
        self.assertEqual("order_update", result.payload["template_name"])
        self.assertEqual("Your order is ready", result.payload["variables"]["body"])


if __name__ == "__main__":
    unittest.main()

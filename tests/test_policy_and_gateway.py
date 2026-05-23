"""Async PolicyGate + WhatsApp gateway adapter tests (in-memory only)."""

from datetime import datetime, timedelta, timezone

import pytest

from services.orchestrator.policy_gate import (
    AuditTrail,
    PatientStateStore,
    PolicyGate,
    ReasonCode,
)
from services.whatsapp_gateway.outbound import (
    FreeformSendAPI,
    TemplateSendAPI,
    WhatsAppGateway,
)


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def gate() -> PolicyGate:
    return PolicyGate(PatientStateStore(), AuditTrail())


async def test_allows_freeform_within_24h_window(gate: PolicyGate, now: datetime):
    await gate.state_store.set_last_inbound_timestamp("p1", now - timedelta(hours=2))
    decision = await gate.evaluate(
        "p1", intent="general_question", requested_flow="support", now=now
    )
    assert decision.allow_freeform is True
    assert decision.outbound_mode == "FREEFORM"
    assert ReasonCode.FREEFORM_ALLOWED_WITHIN_WINDOW in decision.reason_codes


async def test_requires_template_outside_window(gate: PolicyGate, now: datetime):
    await gate.state_store.set_last_inbound_timestamp("p1", now - timedelta(hours=26))
    decision = await gate.evaluate(
        "p1", intent="general_question", requested_flow="support", now=now
    )
    assert decision.allow_freeform is False
    assert decision.outbound_mode == "TEMPLATE"
    assert ReasonCode.TEMPLATE_REQUIRED_OUTSIDE_WINDOW in decision.reason_codes


async def test_rejects_disallowed_medicine_flow_and_exposes_escalation(
    gate: PolicyGate, now: datetime
):
    await gate.state_store.set_last_inbound_timestamp("p1", now - timedelta(hours=2))
    decision = await gate.evaluate(
        "p1",
        intent="medicine_ordering",
        requested_flow="order_controlled_medicine",
        now=now,
    )
    assert decision.flow_action == "REJECT"
    assert ReasonCode.DISALLOWED_MEDICINE_ORDERING_FLOW in decision.reason_codes
    assert "CALL" in decision.escalation_actions


async def test_policy_gate_handles_naive_now_and_dedupes_reason_codes(
    gate: PolicyGate,
):
    naive_now = datetime(2026, 1, 1)
    await gate.state_store.set_last_inbound_timestamp(
        "p3", datetime(2025, 12, 31, 23, 0, 0)
    )

    decision = await gate.evaluate(
        "p3", intent="general_question", requested_flow="support", now=naive_now
    )

    assert decision.outbound_mode in {"FREEFORM", "TEMPLATE"}
    assert len(decision.reason_codes) == len(set(decision.reason_codes))


async def test_audit_trail_rejects_invalid_policy_shapes():
    audit = AuditTrail()

    bad_decision = type(
        "D",
        (),
        {
            "patient_id": "p1",
            "outbound_mode": "BAD",
            "flow_action": "ALLOW",
            "reason_codes": [],
            "details": {},
        },
    )()

    with pytest.raises(ValueError):
        await audit.log_policy_decision(bad_decision)


async def test_whatsapp_gateway_enforces_template_when_policy_requires_template(
    now: datetime,
):
    store = PatientStateStore()
    audit = AuditTrail()
    gate = PolicyGate(store, audit)
    await store.set_last_inbound_timestamp("p2", now - timedelta(hours=48))
    decision = await gate.evaluate(
        "p2", intent="general_question", requested_flow="support", now=now
    )

    gateway = WhatsAppGateway(FreeformSendAPI(), TemplateSendAPI(), audit)
    result = await gateway.send_outbound(
        "p2",
        text="Your order is ready",
        policy_decision=decision,
        template_name="order_update",
        template_variables={"order_id": "123"},
    )

    assert result.mode == "TEMPLATE"
    assert result.payload["template_name"] == "order_update"
    assert result.payload["variables"]["body"] == "Your order is ready"

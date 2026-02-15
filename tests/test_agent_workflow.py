from datetime import datetime, timedelta, timezone

from services.orchestrator.agent_workflow import run_agent_workflow


def test_workflow_outside_csw_requires_template_and_audit_reason():
    now = datetime(2026, 2, 20, 10, 0, tzinfo=timezone.utc)
    result = run_agent_workflow(
        message_id="m1",
        patient_id="p1",
        text="Need refill, I might run out",
        phone="+911234567890",
        last_user_message_at=now - timedelta(hours=30),
        now=now,
    )

    assert result.intent == "refill_request"
    assert result.use_template is True
    assert result.template_name == "escalate_call_v1"
    assert "template_required" in result.audit_reasons


def test_workflow_triages_critical_symptom_to_escalation():
    now = datetime(2026, 2, 20, 10, 0, tzinfo=timezone.utc)
    result = run_agent_workflow(
        message_id="m2",
        patient_id="p2",
        text="I have chest pain and cannot breathe",
        phone=None,
        last_user_message_at=now - timedelta(hours=1),
        now=now,
    )

    assert result.intent == "symptom_report"
    assert result.risk_level == "critical"
    assert result.escalation_required is True
    assert result.escalation_reason == "critical_red_flag"
    assert "CALL now" in result.response_body


def test_workflow_detects_followup_closure_intent():
    now = datetime(2026, 2, 20, 10, 0, tzinfo=timezone.utc)
    result = run_agent_workflow(
        message_id="m3",
        patient_id="p3",
        text="Lab booked",
        phone=None,
        last_user_message_at=now - timedelta(minutes=20),
        now=now,
    )

    assert result.intent == "followup_update"
    assert result.use_template is False
    assert "BOOKED, COMPLETED, or REVIEWED" in result.response_body

from datetime import datetime, timedelta, timezone

from services.orchestrator.main import policy_gate


def test_policy_gate_inside_24h_allows_freeform():
    now = datetime.now(timezone.utc)
    decision = policy_gate(now=now, last_user_message_at=now - timedelta(hours=2))
    assert decision.use_template is False


def test_policy_gate_outside_24h_requires_template():
    now = datetime.now(timezone.utc)
    decision = policy_gate(now=now, last_user_message_at=now - timedelta(hours=26))
    assert decision.use_template is True

from services.scheduler.main import (
    FollowupClosureRequest,
    TriageAlertRequest,
    emit_followup_closure,
    emit_triage_alert,
)
from shared.contracts.enums import EventType


def test_emit_triage_alert_uses_triage_event_type():
    event = emit_triage_alert(
        TriageAlertRequest(
            patient_id="p-123",
            cohort="default",
            severity="high",
            reason="high blood pressure",
        )
    )
    assert event.event_type == EventType.TRIAGE_ALERT


def test_emit_followup_closure_uses_followup_closure_event_type():
    event = emit_followup_closure(
        FollowupClosureRequest(
            patient_id="p-123",
            followup_type="lab",
            item_name="HbA1c",
            status="completed",
        )
    )
    assert event.event_type == EventType.FOLLOWUP_CLOSURE

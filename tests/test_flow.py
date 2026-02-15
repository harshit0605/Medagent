from datetime import datetime, timedelta

from medagent import (
    CAREGIVER_MISSED_STREAK_TEMPLATE,
    DOSE_MISSED_FOLLOWUP_TEMPLATE,
    DOSE_REMINDER_TEMPLATE,
    FakeGateway,
    InMemoryStore,
    InboundParser,
    MedAgentFlow,
    Regimen,
)


def test_parser_normalization_is_deterministic():
    parser = InboundParser()

    assert parser.normalize("Taken") == "taken"
    assert parser.normalize(" taken ") == "taken"
    assert parser.normalize("1") == "taken"

    assert parser.normalize("Snooze") == "snooze"
    assert parser.normalize("2") == "snooze"

    assert parser.normalize("Skip") == "skip"
    assert parser.normalize("3") == "skip"

    assert parser.normalize("unknown") is None


def test_threshold_triggering_opens_alert_and_routes_high_risk():
    store = InMemoryStore()
    gateway = FakeGateway()
    flow = MedAgentFlow(store=store, gateway=gateway, missed_threshold=2)

    due = datetime(2026, 1, 1, 9, 0, 0)
    regimen = Regimen(
        patient_id="patient-1",
        medication="atorvastatin",
        due_at=due,
        caregiver_alerts_enabled=True,
    )

    events = flow.run_scheduler([regimen])
    assert len(events) == 1
    assert gateway.sent[-1].template == DOSE_REMINDER_TEMPLATE

    first = flow.handle_reply(regimen, "Skip", when=due + timedelta(minutes=5))
    assert first == "skip"
    assert len(store.alerts) == 0

    second = flow.handle_reply(regimen, "3", when=due + timedelta(hours=24, minutes=5))
    assert second == "skip"

    assert len(store.alerts) == 1
    assert store.alerts[0].reason == "missed_streak_2"

    sent_templates = [m.template for m in gateway.sent]
    assert DOSE_MISSED_FOLLOWUP_TEMPLATE in sent_templates
    assert CAREGIVER_MISSED_STREAK_TEMPLATE in sent_templates

    assert len(store.human_queue) == 1
    assert store.human_queue[0].reason == "high_risk_missed_doses:2"

from datetime import datetime, timedelta

from medagent import (
    APPOINTMENT_CLOSURE_UPDATE_TEMPLATE,
    CAREGIVER_DAILY_DIGEST_TEMPLATE,
    CAREGIVER_MISSED_STREAK_TEMPLATE,
    DOSE_MISSED_FOLLOWUP_TEMPLATE,
    DOSE_REMINDER_TEMPLATE,
    LAB_CLOSURE_UPDATE_TEMPLATE,
    MISSED_REASON_PROMPT_TEMPLATE,
    REFILL_STAGE_TEMPLATE,
    TRIAGE_ALERT_TEMPLATE,
    FakeGateway,
    InMemoryStore,
    InboundParser,
    MedAgentFlow,
    Regimen,
    RefillForecaster,
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
    regimen = Regimen(patient_id="patient-1", medication="atorvastatin", due_at=due, caregiver_alerts_enabled=True)

    events = flow.run_scheduler([regimen])
    assert len(events) == 1
    assert gateway.sent[-1].template == DOSE_REMINDER_TEMPLATE

    assert flow.handle_reply(regimen, "Skip", when=due + timedelta(minutes=5)) == "skip"
    assert gateway.sent[-1].template == MISSED_REASON_PROMPT_TEMPLATE

    assert flow.handle_reply(regimen, "3", when=due + timedelta(hours=24, minutes=5)) == "skip"
    assert len(store.alerts) == 1

    sent_templates = [m.template for m in gateway.sent]
    assert DOSE_MISSED_FOLLOWUP_TEMPLATE in sent_templates
    assert CAREGIVER_MISSED_STREAK_TEMPLATE in sent_templates
    assert len(store.human_queue) == 1


def test_parser_supports_emoji_and_missed():
    parser = InboundParser()
    assert parser.normalize("✅") == "taken"
    assert parser.normalize("⏰") == "snooze"
    assert parser.normalize("❌") == "skip"
    assert parser.normalize("missed") == "missed"


def test_engine_rejects_invalid_threshold_and_action():
    store = InMemoryStore()
    gateway = FakeGateway()

    try:
        MedAgentFlow(store=store, gateway=gateway, missed_threshold=0)
        assert False
    except ValueError:
        pass

    flow = MedAgentFlow(store=store, gateway=gateway, missed_threshold=2)
    regimen = Regimen(patient_id="patient-1", medication="atorvastatin", due_at=datetime(2026, 1, 1, 9, 0, 0))

    try:
        flow.engine.record_action(regimen, "bad_action", datetime(2026, 1, 1, 9, 5, 0))
        assert False
    except ValueError:
        pass


def test_miss_recovery_reason_routes_to_correct_actions():
    store = InMemoryStore()
    gateway = FakeGateway()
    flow = MedAgentFlow(store=store, gateway=gateway)
    regimen = Regimen(patient_id="patient-1", medication="metformin", due_at=datetime(2026, 1, 1, 9, 0, 0))

    assert flow.handle_missed_reason(regimen, "forgot", datetime(2026, 1, 1, 10, 0, 0)) == "reschedule"
    assert flow.handle_missed_reason(regimen, "out_of_stock", datetime(2026, 1, 1, 10, 1, 0)) == "refill_support"
    assert flow.handle_missed_reason(regimen, "side_effect", datetime(2026, 1, 1, 10, 2, 0)) == "escalate_clinician"

    assert any(e.reason == "side_effect" for e in store.miss_recovery_events)
    assert any(q.reason == "miss_recovery_side_effect" for q in store.human_queue)


def test_refill_forecast_stages_and_prompt_send():
    store = InMemoryStore()
    gateway = FakeGateway()
    flow = MedAgentFlow(store=store, gateway=gateway)

    assert flow.run_refill_check("patient-1", "metformin", days_left=10) is None
    d7 = flow.run_refill_check("patient-1", "metformin", days_left=7)
    d3 = flow.run_refill_check("patient-1", "metformin", days_left=3)
    d1 = flow.run_refill_check("patient-1", "metformin", days_left=1)

    assert d7 and d7.stage == "d7"
    assert d3 and d3.stage == "d3"
    assert d1 and d1.stage == "d1"
    assert len([m for m in gateway.sent if m.template == REFILL_STAGE_TEMPLATE]) == 3


def test_refill_forecaster_normalizes_negative_days_left():
    forecast = RefillForecaster().forecast("patient-1", "metformin", days_left=-4)
    assert forecast is not None
    assert forecast.stage == "d1"


def test_cohort_triage_generates_priority_queue_item():
    store = InMemoryStore()
    gateway = FakeGateway()
    flow = MedAgentFlow(store=store, gateway=gateway)

    decision = flow.run_triage(
        patient_id="patient-7",
        cohort="asthma",
        symptom_text="wheezing with night awakenings",
        when=datetime(2026, 1, 2, 8, 0, 0),
    )

    assert decision.severity == "high"
    assert decision.escalation_required is True
    assert any(m.template == TRIAGE_ALERT_TEMPLATE for m in gateway.sent)
    assert any(q.reason == "triage_asthma_high" and q.priority == "p1" for q in store.human_queue)


def test_caregiver_digest_summarizes_last_24h_misses_and_alerts():
    store = InMemoryStore()
    gateway = FakeGateway()
    flow = MedAgentFlow(store=store, gateway=gateway)

    now = datetime(2026, 1, 3, 9, 0, 0)
    regimen = Regimen(patient_id="patient-99", medication="amlodipine", due_at=now)

    flow.handle_reply(regimen, "skip", now - timedelta(hours=3))
    flow.handle_reply(regimen, "skip", now - timedelta(hours=2))

    digest = flow.build_and_send_caregiver_digest("patient-99", "cg-1", now)
    assert digest.missed_doses_24h >= 2
    assert digest.high_risk_alerts_open >= 1
    assert any(m.template == CAREGIVER_DAILY_DIGEST_TEMPLATE for m in gateway.sent)


def test_caregiver_permissions_persist_in_store():
    store = InMemoryStore()
    flow = MedAgentFlow(store=store, gateway=FakeGateway())
    flow.set_caregiver_permissions("cg-55", can_snooze=True, can_skip=False)
    assert store.caregiver_permissions["cg-55"]["can_snooze"] is True
    assert store.caregiver_permissions["cg-55"]["can_skip"] is False


def test_followup_closure_loops_and_dashboard_metrics():
    store = InMemoryStore()
    gateway = FakeGateway()
    flow = MedAgentFlow(store=store, gateway=gateway)

    now = datetime(2026, 1, 5, 10, 0, 0)
    regimen = Regimen(patient_id="p-clinic", medication="metformin", due_at=now)
    flow.handle_reply(regimen, "taken", now)
    flow.handle_reply(regimen, "skip", now + timedelta(hours=1))
    flow.handle_missed_reason(regimen, "out_of_stock", now + timedelta(hours=2))

    lab = flow.advance_lab_journey("p-clinic", "HbA1c", "booked", now)
    assert lab.status == "booked"
    lab = flow.advance_lab_journey("p-clinic", "HbA1c", "reviewed", now + timedelta(days=1))
    assert lab.status == "reviewed"

    appt = flow.advance_appointment_journey("p-clinic", "Dr. A", "completed", now)
    assert appt.status == "completed"
    appt = flow.advance_appointment_journey("p-clinic", "Dr. A", "reviewed", now + timedelta(days=1))
    assert appt.status == "reviewed"

    dashboard = flow.build_program_dashboard()
    assert dashboard.adherence_rate > 0
    assert dashboard.refill_risk_rate >= 0
    assert dashboard.followup_closure_rate == 1.0

    sent_templates = [m.template for m in gateway.sent]
    assert LAB_CLOSURE_UPDATE_TEMPLATE in sent_templates
    assert APPOINTMENT_CLOSURE_UPDATE_TEMPLATE in sent_templates


def test_ops_ticket_lifecycle_and_snapshot():
    flow = MedAgentFlow(store=InMemoryStore(), gateway=FakeGateway())
    now = datetime(2026, 1, 6, 10, 0, 0)

    t1 = flow.create_ops_ticket(
        patient_id="p-1",
        category="triage",
        priority="p1",
        sla_minutes=15,
        created_at=now,
    )
    t2 = flow.create_ops_ticket(
        patient_id="p-2",
        category="followup",
        priority="p2",
        sla_minutes=60,
        created_at=now,
    )

    flow.acknowledge_ops_ticket(t1.ticket_id, now + timedelta(minutes=2))
    flow.resolve_ops_ticket(t1.ticket_id, now + timedelta(minutes=9), notes="called patient")

    snapshot = flow.ops_queue_snapshot()
    assert snapshot["total"] == 2
    assert snapshot["open"] == 1
    assert snapshot["acknowledged"] == 0
    assert snapshot["resolved"] == 1
    assert flow.store.ops_tickets[t1.ticket_id].notes == "called patient"
    assert flow.store.ops_tickets[t2.ticket_id].status == "open"

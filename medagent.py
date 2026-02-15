from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional


DOSE_REMINDER_TEMPLATE = "dose_reminder_v1"
DOSE_MISSED_FOLLOWUP_TEMPLATE = "dose_missed_followup_v1"
CAREGIVER_MISSED_STREAK_TEMPLATE = "caregiver_missed_streak_v1"
MISSED_REASON_PROMPT_TEMPLATE = "dose_miss_reason_prompt_v1"
REFILL_STAGE_TEMPLATE = "refill_due_v1"
TRIAGE_ALERT_TEMPLATE = "triage_alert_v1"
CAREGIVER_DAILY_DIGEST_TEMPLATE = "caregiver_daily_digest_v1"
LAB_CLOSURE_UPDATE_TEMPLATE = "lab_closure_update_v1"
APPOINTMENT_CLOSURE_UPDATE_TEMPLATE = "appointment_closure_update_v1"

MISSED_RECOVERY_REASONS = {
    "forgot",
    "side_effect",
    "out_of_stock",
    "confused",
    "cost",
    "other",
}

SUPPORTED_COHORTS = {"diabetes", "bp", "asthma", "pregnancy", "post_op"}


@dataclass(frozen=True)
class Regimen:
    patient_id: str
    medication: str
    due_at: datetime
    caregiver_alerts_enabled: bool = False


@dataclass(frozen=True)
class DoseDueEvent:
    patient_id: str
    medication: str
    due_at: datetime


@dataclass(frozen=True)
class AdherenceEvent:
    patient_id: str
    medication: str
    action: str
    occurred_at: datetime


@dataclass(frozen=True)
class Alert:
    patient_id: str
    medication: str
    reason: str
    opened_at: datetime


@dataclass(frozen=True)
class HumanQueueItem:
    patient_id: str
    medication: str
    reason: str
    queued_at: datetime
    priority: str = "normal"
    sla_minutes: int = 120


@dataclass(frozen=True)
class MissRecoveryEvent:
    patient_id: str
    medication: str
    reason: str
    action: str
    occurred_at: datetime


@dataclass(frozen=True)
class RefillForecast:
    patient_id: str
    medication: str
    days_left: int
    stage: str


@dataclass(frozen=True)
class TriageSignal:
    patient_id: str
    cohort: str
    symptom_text: str


@dataclass(frozen=True)
class TriageDecision:
    patient_id: str
    cohort: str
    severity: str
    reason: str
    escalation_required: bool


@dataclass(frozen=True)
class CaregiverDigest:
    patient_id: str
    caregiver_id: str
    missed_doses_24h: int
    high_risk_alerts_open: int
    generated_at: datetime


@dataclass
class LabJourney:
    patient_id: str
    test_name: str
    status: str = "due"
    booked_at: datetime | None = None
    completed_at: datetime | None = None
    reviewed_at: datetime | None = None


@dataclass
class AppointmentJourney:
    patient_id: str
    clinician_name: str
    status: str = "due"
    booked_at: datetime | None = None
    completed_at: datetime | None = None
    reviewed_at: datetime | None = None


@dataclass(frozen=True)
class ProgramDashboard:
    adherence_rate: float
    refill_risk_rate: float
    followup_closure_rate: float


@dataclass
class OpsTicket:
    ticket_id: str
    patient_id: str
    category: str
    priority: str
    sla_minutes: int
    status: str
    created_at: datetime
    acknowledged_at: datetime | None = None
    resolved_at: datetime | None = None
    notes: str | None = None


class InboundParser:
    """Normalize inbound patient responses to canonical adherence actions."""

    _map: Dict[str, str] = {
        "taken": "taken",
        "1": "taken",
        "✅": "taken",
        "snooze": "snooze",
        "2": "snooze",
        "⏰": "snooze",
        "skip": "skip",
        "3": "skip",
        "❌": "skip",
        "missed": "missed",
    }

    def normalize(self, reply: str | None) -> Optional[str]:
        if reply is None:
            return None
        normalized = reply.strip().lower()
        return self._map.get(normalized)


@dataclass
class InMemoryStore:
    adherence_events: List[AdherenceEvent] = field(default_factory=list)
    alerts: List[Alert] = field(default_factory=list)
    human_queue: List[HumanQueueItem] = field(default_factory=list)
    miss_recovery_events: List[MissRecoveryEvent] = field(default_factory=list)
    triage_decisions: List[TriageDecision] = field(default_factory=list)
    caregiver_permissions: Dict[str, Dict[str, bool]] = field(default_factory=dict)
    labs: Dict[str, LabJourney] = field(default_factory=dict)
    appointments: Dict[str, AppointmentJourney] = field(default_factory=dict)
    ops_tickets: Dict[str, OpsTicket] = field(default_factory=dict)

    def add_adherence(self, event: AdherenceEvent) -> None:
        self.adherence_events.append(event)

    def add_miss_recovery(self, event: MissRecoveryEvent) -> None:
        self.miss_recovery_events.append(event)

    def add_triage_decision(self, decision: TriageDecision) -> None:
        self.triage_decisions.append(decision)

    def set_caregiver_permissions(self, caregiver_id: str, can_snooze: bool, can_skip: bool) -> None:
        self.caregiver_permissions[caregiver_id] = {
            "can_snooze": can_snooze,
            "can_skip": can_skip,
        }

    def recent_for_patient_med(self, patient_id: str, medication: str) -> List[AdherenceEvent]:
        return [
            e
            for e in self.adherence_events
            if e.patient_id == patient_id and e.medication == medication
        ]

    def missed_in_last_24h(self, patient_id: str, now: datetime) -> int:
        return sum(
            1
            for e in self.adherence_events
            if e.patient_id == patient_id
            and e.action in {"missed", "skip"}
            and (now - e.occurred_at).total_seconds() <= 86400
        )

    def high_risk_alert_count(self, patient_id: str) -> int:
        return sum(1 for a in self.alerts if a.patient_id == patient_id and "missed_streak" in a.reason)

    def has_open_alert(self, patient_id: str, medication: str, reason: str) -> bool:
        return any(
            a.patient_id == patient_id and a.medication == medication and a.reason == reason
            for a in self.alerts
        )

    def has_human_queue_item(self, patient_id: str, medication: str, reason: str) -> bool:
        return any(
            q.patient_id == patient_id and q.medication == medication and q.reason == reason
            for q in self.human_queue
        )


@dataclass
class GatewayMessage:
    to: str
    template: str
    payload: Dict[str, str]


@dataclass
class FakeGateway:
    sent: List[GatewayMessage] = field(default_factory=list)

    def send_template(self, to: str, template: str, payload: Dict[str, str]) -> None:
        self.sent.append(GatewayMessage(to=to, template=template, payload=payload))


class Scheduler:
    def emit_dose_due(self, regimens: List[Regimen]) -> List[DoseDueEvent]:
        return [
            DoseDueEvent(
                patient_id=reg.patient_id,
                medication=reg.medication,
                due_at=reg.due_at,
            )
            for reg in regimens
        ]


class RefillForecaster:
    """Simple stage forecaster for D-7 / D-3 / D-1 reminder ladder."""

    def forecast(self, patient_id: str, medication: str, days_left: int) -> Optional[RefillForecast]:
        stage = self.stage_for_days_left(days_left)
        if stage is None:
            return None
        return RefillForecast(patient_id=patient_id, medication=medication, days_left=days_left, stage=stage)

    @staticmethod
    def stage_for_days_left(days_left: int) -> Optional[str]:
        if days_left < 0:
            days_left = 0
        if days_left <= 1:
            return "d1"
        if days_left <= 3:
            return "d3"
        if days_left <= 7:
            return "d7"
        return None


class TriageAssessor:
    """Simple cohort-aware triage classifier for safety-first routing."""

    _critical_keywords = {"unconscious", "chest pain", "severe bleeding", "cannot breathe"}

    _high_keywords = {
        "diabetes": {"hypo", "very high sugar", "confusion"},
        "bp": {"very high bp", "severe headache", "chest pain"},
        "asthma": {"wheezing", "night awakenings", "breathless"},
        "pregnancy": {"bleeding", "severe pain", "reduced fetal movement"},
        "post_op": {"fever", "pus", "severe pain"},
    }

    def assess(self, signal: TriageSignal) -> TriageDecision:
        cohort = signal.cohort.strip().lower()
        if cohort not in SUPPORTED_COHORTS:
            raise ValueError(f"Unsupported cohort: {cohort}")

        text = signal.symptom_text.strip().lower()
        severity = "low"
        reason = "no_red_flag"

        if any(k in text for k in self._critical_keywords):
            severity = "critical"
            reason = "critical_red_flag"
        elif any(k in text for k in self._high_keywords.get(cohort, set())):
            severity = "high"
            reason = f"{cohort}_high_risk_signal"
        elif any(k in text for k in {"pain", "dizzy", "nausea", "weak"}):
            severity = "medium"
            reason = "symptom_monitoring"

        return TriageDecision(
            patient_id=signal.patient_id,
            cohort=cohort,
            severity=severity,
            reason=reason,
            escalation_required=severity in {"high", "critical"},
        )


class OpsPrioritizer:
    _map = {
        "critical": ("p0", 5),
        "high": ("p1", 15),
        "medium": ("p2", 60),
        "low": ("p3", 240),
    }

    def priority_for(self, severity: str) -> tuple[str, int]:
        return self._map.get(severity, ("p3", 240))


class AdherenceEngine:
    def __init__(self, store: InMemoryStore, gateway: FakeGateway, missed_threshold: int = 2):
        if missed_threshold < 1:
            raise ValueError("missed_threshold must be >= 1")
        self.store = store
        self.gateway = gateway
        self.missed_threshold = missed_threshold

    def send_reminder(self, event: DoseDueEvent) -> None:
        self.gateway.send_template(
            to=event.patient_id,
            template=DOSE_REMINDER_TEMPLATE,
            payload={"medication": event.medication, "due_at": event.due_at.isoformat()},
        )

    def send_refill_stage_prompt(self, forecast: RefillForecast) -> None:
        self.gateway.send_template(
            to=forecast.patient_id,
            template=REFILL_STAGE_TEMPLATE,
            payload={
                "medication": forecast.medication,
                "days_left": str(forecast.days_left),
                "stage": forecast.stage,
                "actions": "REORDER/UPDATE COUNT",
            },
        )

    def send_triage_alert(self, decision: TriageDecision) -> None:
        self.gateway.send_template(
            to=decision.patient_id,
            template=TRIAGE_ALERT_TEMPLATE,
            payload={
                "cohort": decision.cohort,
                "severity": decision.severity,
                "reason": decision.reason,
                "actions": "CALL/HELP",
            },
        )

    def send_caregiver_digest(self, digest: CaregiverDigest) -> None:
        self.gateway.send_template(
            to=f"caregiver:{digest.caregiver_id}",
            template=CAREGIVER_DAILY_DIGEST_TEMPLATE,
            payload={
                "patient_id": digest.patient_id,
                "missed_doses_24h": str(digest.missed_doses_24h),
                "high_risk_alerts_open": str(digest.high_risk_alerts_open),
                "generated_at": digest.generated_at.isoformat(),
            },
        )

    def send_lab_closure_update(self, patient_id: str, test_name: str, status: str) -> None:
        self.gateway.send_template(
            to=patient_id,
            template=LAB_CLOSURE_UPDATE_TEMPLATE,
            payload={"test_name": test_name, "status": status},
        )

    def send_appointment_closure_update(self, patient_id: str, clinician_name: str, status: str) -> None:
        self.gateway.send_template(
            to=patient_id,
            template=APPOINTMENT_CLOSURE_UPDATE_TEMPLATE,
            payload={"clinician": clinician_name, "status": status},
        )

    def record_action(self, regimen: Regimen, action: str, when: datetime) -> None:
        if action not in {"taken", "snooze", "skip", "missed"}:
            raise ValueError(f"Unsupported adherence action: {action}")
        adherence = AdherenceEvent(
            patient_id=regimen.patient_id,
            medication=regimen.medication,
            action=action,
            occurred_at=when,
        )
        self.store.add_adherence(adherence)
        self._evaluate_missed_dose_pattern(regimen, when)

    def recover_missed_dose(self, regimen: Regimen, reason: str, when: datetime) -> str:
        if reason not in MISSED_RECOVERY_REASONS:
            raise ValueError(f"Unsupported missed-dose reason: {reason}")

        if reason == "forgot":
            action = "reschedule"
        elif reason in {"side_effect", "confused"}:
            action = "escalate_clinician"
            queue_reason = f"miss_recovery_{reason}"
            if not self.store.has_human_queue_item(regimen.patient_id, regimen.medication, queue_reason):
                self.store.human_queue.append(
                    HumanQueueItem(
                        patient_id=regimen.patient_id,
                        medication=regimen.medication,
                        reason=queue_reason,
                        queued_at=when,
                        priority="p1",
                        sla_minutes=15,
                    )
                )
        elif reason in {"out_of_stock", "cost"}:
            action = "refill_support"
        else:
            action = "human_review"

        self.store.add_miss_recovery(
            MissRecoveryEvent(
                patient_id=regimen.patient_id,
                medication=regimen.medication,
                reason=reason,
                action=action,
                occurred_at=when,
            )
        )
        return action

    def _evaluate_missed_dose_pattern(self, regimen: Regimen, when: datetime) -> None:
        events = self.store.recent_for_patient_med(regimen.patient_id, regimen.medication)
        missed_streak = 0
        for event in reversed(events):
            if event.action == "taken":
                break
            if event.action in {"skip", "missed"}:
                missed_streak += 1
            else:
                break

        if missed_streak < self.missed_threshold:
            if events and events[-1].action in {"skip", "missed"}:
                self.gateway.send_template(
                    to=regimen.patient_id,
                    template=MISSED_REASON_PROMPT_TEMPLATE,
                    payload={
                        "medication": regimen.medication,
                        "actions": "FORGOT/SIDE_EFFECT/OUT_OF_STOCK/CONFUSED/COST/OTHER",
                    },
                )
            return

        reason = f"missed_streak_{missed_streak}"
        if self.store.has_open_alert(regimen.patient_id, regimen.medication, reason):
            return

        self.store.alerts.append(
            Alert(
                patient_id=regimen.patient_id,
                medication=regimen.medication,
                reason=reason,
                opened_at=when,
            )
        )
        self.gateway.send_template(
            to=regimen.patient_id,
            template=DOSE_MISSED_FOLLOWUP_TEMPLATE,
            payload={"medication": regimen.medication, "streak": str(missed_streak)},
        )

        if regimen.caregiver_alerts_enabled:
            self.gateway.send_template(
                to=f"caregiver:{regimen.patient_id}",
                template=CAREGIVER_MISSED_STREAK_TEMPLATE,
                payload={"medication": regimen.medication, "streak": str(missed_streak)},
            )

        if missed_streak >= self.missed_threshold:
            queue_reason = f"high_risk_missed_doses:{missed_streak}"
            if not self.store.has_human_queue_item(regimen.patient_id, regimen.medication, queue_reason):
                self.store.human_queue.append(
                    HumanQueueItem(
                        patient_id=regimen.patient_id,
                        medication=regimen.medication,
                        reason=queue_reason,
                        queued_at=when,
                        priority="p1",
                        sla_minutes=15,
                    )
                )


class MedAgentFlow:
    def __init__(self, store: InMemoryStore, gateway: FakeGateway, missed_threshold: int = 2):
        self.scheduler = Scheduler()
        self.parser = InboundParser()
        self.engine = AdherenceEngine(store=store, gateway=gateway, missed_threshold=missed_threshold)
        self.refill_forecaster = RefillForecaster()
        self.triage_assessor = TriageAssessor()
        self.ops_prioritizer = OpsPrioritizer()
        self.store = store

    def run_scheduler(self, regimens: List[Regimen]) -> List[DoseDueEvent]:
        events = self.scheduler.emit_dose_due(regimens)
        for event in events:
            self.engine.send_reminder(event)
        return events

    def handle_reply(self, regimen: Regimen, reply: str, when: datetime) -> Optional[str]:
        action = self.parser.normalize(reply)
        if action is None:
            return None
        self.engine.record_action(regimen=regimen, action=action, when=when)
        return action

    def handle_missed_reason(self, regimen: Regimen, reason: str, when: datetime) -> str:
        normalized_reason = reason.strip().lower()
        return self.engine.recover_missed_dose(regimen=regimen, reason=normalized_reason, when=when)

    def run_refill_check(self, patient_id: str, medication: str, days_left: int) -> Optional[RefillForecast]:
        forecast = self.refill_forecaster.forecast(
            patient_id=patient_id,
            medication=medication,
            days_left=days_left,
        )
        if forecast is None:
            return None
        self.engine.send_refill_stage_prompt(forecast)
        return forecast

    def run_triage(self, patient_id: str, cohort: str, symptom_text: str, when: datetime) -> TriageDecision:
        decision = self.triage_assessor.assess(
            TriageSignal(patient_id=patient_id, cohort=cohort, symptom_text=symptom_text)
        )
        self.store.add_triage_decision(decision)
        self.engine.send_triage_alert(decision)

        if decision.escalation_required:
            priority, sla_minutes = self.ops_prioritizer.priority_for(decision.severity)
            queue_reason = f"triage_{decision.cohort}_{decision.severity}"
            if not self.store.has_human_queue_item(patient_id, medication="triage", reason=queue_reason):
                self.store.human_queue.append(
                    HumanQueueItem(
                        patient_id=patient_id,
                        medication="triage",
                        reason=queue_reason,
                        queued_at=when,
                        priority=priority,
                        sla_minutes=sla_minutes,
                    )
                )

        return decision

    def build_and_send_caregiver_digest(self, patient_id: str, caregiver_id: str, now: datetime) -> CaregiverDigest:
        digest = CaregiverDigest(
            patient_id=patient_id,
            caregiver_id=caregiver_id,
            missed_doses_24h=self.store.missed_in_last_24h(patient_id, now),
            high_risk_alerts_open=self.store.high_risk_alert_count(patient_id),
            generated_at=now,
        )
        self.engine.send_caregiver_digest(digest)
        return digest

    def set_caregiver_permissions(self, caregiver_id: str, can_snooze: bool, can_skip: bool) -> None:
        self.store.set_caregiver_permissions(caregiver_id, can_snooze=can_snooze, can_skip=can_skip)

    def upsert_lab_journey(self, patient_id: str, test_name: str) -> None:
        key = f"{patient_id}:{test_name}"
        if key not in self.store.labs:
            self.store.labs[key] = LabJourney(patient_id=patient_id, test_name=test_name)

    def advance_lab_journey(self, patient_id: str, test_name: str, status: str, when: datetime) -> LabJourney:
        if status not in {"booked", "completed", "reviewed"}:
            raise ValueError("invalid lab status")
        self.upsert_lab_journey(patient_id, test_name)
        journey = self.store.labs[f"{patient_id}:{test_name}"]
        journey.status = status
        if status == "booked":
            journey.booked_at = when
        elif status == "completed":
            journey.completed_at = when
        elif status == "reviewed":
            journey.reviewed_at = when
        self.engine.send_lab_closure_update(patient_id, test_name, status)
        return journey

    def upsert_appointment_journey(self, patient_id: str, clinician_name: str) -> None:
        key = f"{patient_id}:{clinician_name}"
        if key not in self.store.appointments:
            self.store.appointments[key] = AppointmentJourney(patient_id=patient_id, clinician_name=clinician_name)

    def advance_appointment_journey(
        self, patient_id: str, clinician_name: str, status: str, when: datetime
    ) -> AppointmentJourney:
        if status not in {"booked", "completed", "reviewed"}:
            raise ValueError("invalid appointment status")
        self.upsert_appointment_journey(patient_id, clinician_name)
        journey = self.store.appointments[f"{patient_id}:{clinician_name}"]
        journey.status = status
        if status == "booked":
            journey.booked_at = when
        elif status == "completed":
            journey.completed_at = when
        elif status == "reviewed":
            journey.reviewed_at = when
        self.engine.send_appointment_closure_update(patient_id, clinician_name, status)
        return journey

    def build_program_dashboard(self) -> ProgramDashboard:
        total_adherence = len(self.store.adherence_events)
        adherence_taken = sum(1 for e in self.store.adherence_events if e.action == "taken")
        adherence_rate = adherence_taken / total_adherence if total_adherence else 0.0

        total_refills = len([e for e in self.store.miss_recovery_events if e.action in {"refill_support", "reschedule"}])
        refill_risk = len([e for e in self.store.miss_recovery_events if e.action == "refill_support"])
        refill_risk_rate = refill_risk / total_refills if total_refills else 0.0

        closed_labs = sum(1 for j in self.store.labs.values() if j.status == "reviewed")
        closed_appts = sum(1 for j in self.store.appointments.values() if j.status == "reviewed")
        total_followups = len(self.store.labs) + len(self.store.appointments)
        followup_closure_rate = ((closed_labs + closed_appts) / total_followups) if total_followups else 0.0

        return ProgramDashboard(
            adherence_rate=round(adherence_rate, 4),
            refill_risk_rate=round(refill_risk_rate, 4),
            followup_closure_rate=round(followup_closure_rate, 4),
        )


    def create_ops_ticket(
        self,
        patient_id: str,
        category: str,
        priority: str,
        sla_minutes: int,
        created_at: datetime,
        notes: str | None = None,
    ) -> OpsTicket:
        ticket_id = f"ticket_{len(self.store.ops_tickets) + 1}"
        ticket = OpsTicket(
            ticket_id=ticket_id,
            patient_id=patient_id,
            category=category,
            priority=priority,
            sla_minutes=sla_minutes,
            status="open",
            created_at=created_at,
            notes=notes,
        )
        self.store.ops_tickets[ticket_id] = ticket
        return ticket

    def acknowledge_ops_ticket(self, ticket_id: str, at: datetime) -> OpsTicket:
        ticket = self.store.ops_tickets[ticket_id]
        ticket.status = "acknowledged"
        ticket.acknowledged_at = at
        return ticket

    def resolve_ops_ticket(self, ticket_id: str, at: datetime, notes: str | None = None) -> OpsTicket:
        ticket = self.store.ops_tickets[ticket_id]
        ticket.status = "resolved"
        ticket.resolved_at = at
        if notes:
            ticket.notes = notes
        return ticket

    def ops_queue_snapshot(self) -> dict[str, int]:
        open_count = sum(1 for t in self.store.ops_tickets.values() if t.status == "open")
        acknowledged_count = sum(1 for t in self.store.ops_tickets.values() if t.status == "acknowledged")
        resolved_count = sum(1 for t in self.store.ops_tickets.values() if t.status == "resolved")
        return {
            "open": open_count,
            "acknowledged": acknowledged_count,
            "resolved": resolved_count,
            "total": len(self.store.ops_tickets),
        }

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional


DOSE_REMINDER_TEMPLATE = "dose_reminder_v1"
DOSE_MISSED_FOLLOWUP_TEMPLATE = "dose_missed_followup_v1"
CAREGIVER_MISSED_STREAK_TEMPLATE = "caregiver_missed_streak_v1"


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


class InboundParser:
    """Normalize inbound patient responses to canonical adherence actions."""

    _map: Dict[str, str] = {
        "taken": "taken",
        "1": "taken",
        "snooze": "snooze",
        "2": "snooze",
        "skip": "skip",
        "3": "skip",
    }

    def normalize(self, reply: str) -> Optional[str]:
        if reply is None:
            return None
        normalized = reply.strip().lower()
        return self._map.get(normalized)


@dataclass
class InMemoryStore:
    adherence_events: List[AdherenceEvent] = field(default_factory=list)
    alerts: List[Alert] = field(default_factory=list)
    human_queue: List[HumanQueueItem] = field(default_factory=list)

    def add_adherence(self, event: AdherenceEvent) -> None:
        self.adherence_events.append(event)

    def recent_for_patient_med(self, patient_id: str, medication: str) -> List[AdherenceEvent]:
        return [
            e
            for e in self.adherence_events
            if e.patient_id == patient_id and e.medication == medication
        ]

    def has_open_alert(self, patient_id: str, medication: str, reason: str) -> bool:
        return any(
            a.patient_id == patient_id and a.medication == medication and a.reason == reason
            for a in self.alerts
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


class AdherenceEngine:
    def __init__(self, store: InMemoryStore, gateway: FakeGateway, missed_threshold: int = 2):
        self.store = store
        self.gateway = gateway
        self.missed_threshold = missed_threshold

    def send_reminder(self, event: DoseDueEvent) -> None:
        self.gateway.send_template(
            to=event.patient_id,
            template=DOSE_REMINDER_TEMPLATE,
            payload={"medication": event.medication, "due_at": event.due_at.isoformat()},
        )

    def record_action(self, regimen: Regimen, action: str, when: datetime) -> None:
        adherence = AdherenceEvent(
            patient_id=regimen.patient_id,
            medication=regimen.medication,
            action=action,
            occurred_at=when,
        )
        self.store.add_adherence(adherence)
        self._evaluate_missed_dose_pattern(regimen, when)

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
            self.store.human_queue.append(
                HumanQueueItem(
                    patient_id=regimen.patient_id,
                    medication=regimen.medication,
                    reason=f"high_risk_missed_doses:{missed_streak}",
                    queued_at=when,
                )
            )


class MedAgentFlow:
    def __init__(self, store: InMemoryStore, gateway: FakeGateway, missed_threshold: int = 2):
        self.scheduler = Scheduler()
        self.parser = InboundParser()
        self.engine = AdherenceEngine(store=store, gateway=gateway, missed_threshold=missed_threshold)

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

"""Unit tests for the orchestrator dose handler.

Mocks the DB session + repos so we cover only the action-routing logic.
"""

from __future__ import annotations

import types
from datetime import datetime, timezone

import pytest

from app.db.models import AdherenceStatus
from services.orchestrator import dose_handler


class _NoopAsyncSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None

    async def commit(self):
        return None

    async def get(self, *_a, **_k):
        return None


def _patch_session(monkeypatch):
    def factory():
        return _NoopAsyncSession()

    monkeypatch.setattr(dose_handler, "get_sessionmaker", lambda: factory)


def _adherence(*, id=42, status=AdherenceStatus.scheduled, patient_id=2):
    return types.SimpleNamespace(
        id=id,
        status=status,
        patient_id=patient_id,
        regimen_id=1,
        scheduled_at=datetime.now(timezone.utc),
    )


def _regimen():
    return types.SimpleNamespace(
        id=1, medication_name="Metformin", dose="500 mg"
    )


def _patient(*, id=2, phone="9100"):
    return types.SimpleNamespace(id=id, phone=phone)


def _stub_repos(monkeypatch, *, adherence, patient, regimen=None):
    captured = {"updates": []}

    async def get_adh(_db, _id):
        return adherence

    async def get_patient(_db, _phone):
        return patient

    async def get_reg(_db, _id):
        return regimen

    async def mark_taken(_db, _id, **kwargs):
        captured["updates"].append(("taken", _id, kwargs))
        return adherence

    async def mark_skipped(_db, _id, **kwargs):
        captured["updates"].append(("skipped", _id, kwargs))
        return adherence

    async def mark_delayed(_db, _id, **kwargs):
        captured["updates"].append(("delayed", _id, kwargs))
        return adherence

    async def enqueue(_db, **kwargs):
        captured["enqueued"] = kwargs
        return types.SimpleNamespace(id=999)

    monkeypatch.setattr(dose_handler.adherence_events_repo, "get", get_adh)
    monkeypatch.setattr(dose_handler.patients_repo, "get_by_phone", get_patient)
    monkeypatch.setattr(dose_handler.regimens_repo, "get", get_reg)
    monkeypatch.setattr(
        dose_handler.adherence_events_repo, "mark_taken", mark_taken
    )
    monkeypatch.setattr(
        dose_handler.adherence_events_repo, "mark_skipped", mark_skipped
    )
    monkeypatch.setattr(
        dose_handler.adherence_events_repo, "mark_delayed", mark_delayed
    )
    monkeypatch.setattr(dose_handler.scheduled_events_repo, "enqueue", enqueue)
    return captured


def test_looks_like_dose_action_recognises_marker():
    assert dose_handler.looks_like_dose_action(
        "[dose-action] taken adherence_event_id=42"
    )
    assert dose_handler.looks_like_dose_action(
        "[dose-action] snoozed adherence_event_id=7"
    )
    assert dose_handler.looks_like_dose_action(
        "[dose-action] skipped adherence_event_id=1"
    )
    assert dose_handler.looks_like_dose_action(
        "[dose-action] late_taken adherence_event_id=42"
    )
    # Plain text — not a dose action.
    assert not dose_handler.looks_like_dose_action("I took my pill")
    assert not dose_handler.looks_like_dose_action("dose-action taken 42")
    assert not dose_handler.looks_like_dose_action("")


async def test_handle_taken_marks_adherence_and_returns_confirmation(monkeypatch):
    captured = _stub_repos(
        monkeypatch, adherence=_adherence(), patient=_patient(), regimen=_regimen()
    )
    _patch_session(monkeypatch)

    delta = await dose_handler.handle_dose_action(
        patient_phone="9100",
        new_user_text="[dose-action] taken adherence_event_id=42",
    )
    assert delta is not None
    assert "taken" in delta["response_body"].lower()
    assert "Metformin" in delta["response_body"]
    assert delta["intent"] == "adherence_update"
    actions = [u[0] for u in captured["updates"]]
    assert actions == ["taken"]


async def test_handle_skipped_marks_adherence_skipped(monkeypatch):
    captured = _stub_repos(
        monkeypatch, adherence=_adherence(), patient=_patient(), regimen=_regimen()
    )
    _patch_session(monkeypatch)

    delta = await dose_handler.handle_dose_action(
        patient_phone="9100",
        new_user_text="[dose-action] skipped adherence_event_id=42",
    )
    assert delta is not None
    assert "skipped" in delta["response_body"].lower()
    actions = [u[0] for u in captured["updates"]]
    assert actions == ["skipped"]


async def test_handle_snoozed_marks_delayed_and_enqueues_followup(monkeypatch):
    captured = _stub_repos(
        monkeypatch, adherence=_adherence(), patient=_patient(), regimen=_regimen()
    )
    _patch_session(monkeypatch)

    delta = await dose_handler.handle_dose_action(
        patient_phone="9100",
        new_user_text="[dose-action] snoozed adherence_event_id=42",
    )
    assert delta is not None
    assert "30 minutes" in delta["response_body"]
    actions = [u[0] for u in captured["updates"]]
    assert actions == ["delayed"]
    # A follow-up dose_due ScheduledEvent should be enqueued for ~30 min later.
    assert "enqueued" in captured
    assert captured["enqueued"]["event_type"] == "dose_due"
    assert (
        captured["enqueued"]["payload"]["adherence_event_id"] == 42
    )


async def test_handle_refuses_cross_patient(monkeypatch):
    """If the inbound came from phone 'X' but the adherence row belongs to a
    different patient, refuse the update — defense against malformed taps."""
    captured = _stub_repos(
        monkeypatch,
        adherence=_adherence(patient_id=99),  # owned by patient 99
        patient=_patient(id=2, phone="9100"),  # but inbound is from patient 2
        regimen=_regimen(),
    )
    _patch_session(monkeypatch)

    delta = await dose_handler.handle_dose_action(
        patient_phone="9100",
        new_user_text="[dose-action] taken adherence_event_id=42",
    )
    assert delta is not None
    assert "your own account" in delta["response_body"]
    assert "dose_action_cross_patient_refused" in delta["audit_reasons"]
    assert captured["updates"] == []


async def test_handle_already_taken_is_silent_no_button(monkeypatch):
    """Tapping Taken again on an already-taken dose: short, no button —
    nothing meaningful for the patient to do."""
    captured = _stub_repos(
        monkeypatch,
        adherence=_adherence(status=AdherenceStatus.taken),
        patient=_patient(),
        regimen=_regimen(),
    )
    _patch_session(monkeypatch)

    delta = await dose_handler.handle_dose_action(
        patient_phone="9100",
        new_user_text="[dose-action] taken adherence_event_id=42",
    )
    assert delta is not None
    assert "already" in delta["response_body"].lower()
    assert delta["buttons"] == []
    assert captured["updates"] == []


async def test_handle_already_missed_offers_late_taken_button(monkeypatch):
    """Tapping any action on a dose already swept as missed: softened
    'on time matters' message PLUS a Mark-as-taken button as the recovery
    path — patient can still correct adherence record with a single tap."""
    captured = _stub_repos(
        monkeypatch,
        adherence=_adherence(status=AdherenceStatus.missed),
        patient=_patient(),
        regimen=_regimen(),
    )
    _patch_session(monkeypatch)

    delta = await dose_handler.handle_dose_action(
        patient_phone="9100",
        new_user_text="[dose-action] skipped adherence_event_id=42",
    )
    assert delta is not None
    assert "missed" in delta["response_body"].lower()
    assert "tap on time" in delta["response_body"].lower()
    assert len(delta["buttons"]) == 1
    btn = delta["buttons"][0]
    assert btn["label"] == "Mark as taken"
    assert btn["id"] == "dose_late_taken:42"
    # Reply only — does NOT mutate the underlying row.
    assert captured["updates"] == []


async def test_handle_late_taken_overrides_missed_to_taken(monkeypatch):
    """The patient taps Mark-as-taken on a missed dose: status updates to
    taken with late_confirmed=true metadata so reports can distinguish."""
    captured = _stub_repos(
        monkeypatch,
        adherence=_adherence(status=AdherenceStatus.missed),
        patient=_patient(),
        regimen=_regimen(),
    )
    # Add the generic update_status stub the late-taken path needs.

    async def update_status(_db, _id, **kwargs):
        captured["updates"].append(("update_status", _id, kwargs))
        return None

    monkeypatch.setattr(
        dose_handler.adherence_events_repo, "update_status", update_status
    )
    _patch_session(monkeypatch)

    delta = await dose_handler.handle_dose_action(
        patient_phone="9100",
        new_user_text="[dose-action] late_taken adherence_event_id=42",
    )
    assert delta is not None
    assert "logged late" in delta["response_body"].lower()
    assert "Metformin" in delta["response_body"]
    assert delta["audit_reasons"] == ["dose_action_late_taken"]
    actions = [u[0] for u in captured["updates"]]
    assert actions == ["update_status"]
    kwargs = captured["updates"][0][2]
    assert kwargs["status"] == AdherenceStatus.taken
    assert kwargs["metadata"]["late_confirmed"] is True
    assert kwargs["metadata"]["previous_status"] == "missed"


async def test_late_taken_on_already_taken_is_noop(monkeypatch):
    """Tapping Mark-as-taken on an already-taken dose: idempotent, no DB
    change, friendly confirmation."""
    captured = _stub_repos(
        monkeypatch,
        adherence=_adherence(status=AdherenceStatus.taken),
        patient=_patient(),
        regimen=_regimen(),
    )
    _patch_session(monkeypatch)

    delta = await dose_handler.handle_dose_action(
        patient_phone="9100",
        new_user_text="[dose-action] late_taken adherence_event_id=42",
    )
    assert delta is not None
    assert "already" in delta["response_body"].lower()
    assert (
        "dose_action_late_taken_already_taken" in delta["audit_reasons"]
    )


async def test_handle_returns_none_when_marker_absent(monkeypatch):
    """Caller pre-check is meant to gate this, but defensively the handler
    returns None for non-marker text rather than making a DB call."""
    _patch_session(monkeypatch)
    out = await dose_handler.handle_dose_action(
        patient_phone="9100", new_user_text="just a plain message"
    )
    assert out is None

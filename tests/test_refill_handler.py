"""Unit tests for the orchestrator refill handler.

Mocks the DB session + repos so we cover the action-routing logic only.
"""

from __future__ import annotations

import types
from datetime import date


from services.orchestrator import refill_handler


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

    monkeypatch.setattr(refill_handler, "get_sessionmaker", lambda: factory)


def _regimen(*, id=1, patient_id=2, days=30, started=date(2026, 5, 1)):
    return types.SimpleNamespace(
        id=id,
        patient_id=patient_id,
        medication_name="Metformin",
        dose="500 mg",
        schedule={"timezone": "UTC", "times": ["08:00"], "type": "times_of_day"},
        starts_on=None,
        ends_on=None,
        supply_days_initial=days,
        supply_started_on=started,
    )


def _patient(id=2, phone="9100"):
    return types.SimpleNamespace(id=id, phone=phone)


def _stub_repos(monkeypatch, *, regimen, patient, open_help_ticket=None):
    captured = {"calls": []}

    async def get_regimen(_db, _id):
        return regimen

    async def get_patient_by_phone(_db, _phone):
        return patient

    async def reset_supply(_db, _id, **kwargs):
        captured["calls"].append(("reset_supply", _id, kwargs))
        return regimen

    async def cancel_for_regimen(_db, **kwargs):
        captured["calls"].append(("cancel_for_regimen", kwargs))
        return 0

    async def enqueue(_db, **kwargs):
        captured["calls"].append(("enqueue", kwargs))
        return types.SimpleNamespace(id=999)

    async def find_open(_db, *, patient_id, category):
        return open_help_ticket

    async def create_ticket(_db, **kwargs):
        captured["calls"].append(("create_ticket", kwargs))
        return types.SimpleNamespace(id=42, **kwargs)

    monkeypatch.setattr(refill_handler.regimens_repo, "get", get_regimen)
    monkeypatch.setattr(
        refill_handler.patients_repo, "get_by_phone", get_patient_by_phone
    )
    monkeypatch.setattr(
        refill_handler.regimens_repo, "reset_supply", reset_supply
    )
    monkeypatch.setattr(
        refill_handler.refill_reminders, "cancel_for_regimen", cancel_for_regimen
    )
    monkeypatch.setattr(
        refill_handler.scheduled_events_repo, "enqueue", enqueue
    )
    monkeypatch.setattr(
        refill_handler.ops_tickets_repo,
        "find_open_for_patient_category",
        find_open,
    )
    monkeypatch.setattr(refill_handler.ops_tickets_repo, "create", create_ticket)
    return captured


def test_looks_like_refill_action_recognises_marker():
    assert refill_handler.looks_like_refill_action(
        "[refill-action] done regimen_id=4"
    )
    assert refill_handler.looks_like_refill_action(
        "[refill-action] snoozed regimen_id=4"
    )
    assert refill_handler.looks_like_refill_action(
        "[refill-action] help regimen_id=4"
    )
    assert not refill_handler.looks_like_refill_action("I refilled my meds")
    assert not refill_handler.looks_like_refill_action("")


async def test_handle_done_resets_supply_and_cancels_pending(monkeypatch):
    captured = _stub_repos(
        monkeypatch, regimen=_regimen(), patient=_patient()
    )
    _patch_session(monkeypatch)

    delta = await refill_handler.handle_refill_action(
        patient_phone="9100",
        new_user_text="[refill-action] done regimen_id=1",
    )
    assert delta is not None
    assert "refilled" in delta["response_body"].lower()
    assert delta["audit_reasons"] == ["refill_action_done"]
    actions = [c[0] for c in captured["calls"]]
    # Both supply reset AND old-cycle cancel must run.
    assert "reset_supply" in actions
    assert "cancel_for_regimen" in actions


async def test_handle_snoozed_enqueues_next_day_reminder(monkeypatch):
    captured = _stub_repos(
        monkeypatch, regimen=_regimen(), patient=_patient()
    )

    # Stub the snooze count → 0 so we don't trip the cap.
    async def count_snoozes(_db, *, regimen_id, cycle_key):
        return 0

    monkeypatch.setattr(
        refill_handler.refill_reminders,
        "count_snoozes_for_cycle",
        count_snoozes,
    )
    _patch_session(monkeypatch)

    delta = await refill_handler.handle_refill_action(
        patient_phone="9100",
        new_user_text="[refill-action] snoozed regimen_id=1",
    )
    assert delta is not None
    assert "tomorrow" in delta["response_body"].lower()
    enqueue_calls = [c for c in captured["calls"] if c[0] == "enqueue"]
    assert len(enqueue_calls) == 1
    payload = enqueue_calls[0][1]["payload"]
    assert payload["stage"] == "snooze1d"
    # Supply NOT reset on snooze — patient is just deferring the reminder.
    assert not any(c[0] == "reset_supply" for c in captured["calls"])
    # Cap NOT hit → no help ticket.
    assert not any(c[0] == "create_ticket" for c in captured["calls"])


async def test_snooze_cap_converts_to_refill_help_ticket(monkeypatch):
    """Beyond the per-cycle snooze cap, an additional Snooze tap opens an
    ops_help ticket instead of enqueueing yet another snooze1d event."""
    captured = _stub_repos(
        monkeypatch, regimen=_regimen(), patient=_patient()
    )

    # Patient is at the cap (3 snoozes already this cycle).
    async def count_snoozes(_db, *, regimen_id, cycle_key):
        return refill_handler.SNOOZE_CAP_PER_CYCLE

    monkeypatch.setattr(
        refill_handler.refill_reminders,
        "count_snoozes_for_cycle",
        count_snoozes,
    )
    _patch_session(monkeypatch)

    delta = await refill_handler.handle_refill_action(
        patient_phone="9100",
        new_user_text="[refill-action] snoozed regimen_id=1",
    )
    assert delta is not None
    assert "flagged this for our team" in delta["response_body"].lower()
    assert delta["audit_reasons"] == ["refill_action_snooze_cap_hit"]
    # Help ticket was created…
    ticket_calls = [c for c in captured["calls"] if c[0] == "create_ticket"]
    assert len(ticket_calls) == 1
    assert ticket_calls[0][1]["category"] == "refill_help"
    # …and NO new snooze1d event was enqueued.
    assert not any(c[0] == "enqueue" for c in captured["calls"])


async def test_snooze_cap_idempotent_when_help_ticket_already_open(monkeypatch):
    captured = _stub_repos(
        monkeypatch,
        regimen=_regimen(),
        patient=_patient(),
        open_help_ticket=types.SimpleNamespace(id=11),
    )

    async def count_snoozes(_db, *, regimen_id, cycle_key):
        return refill_handler.SNOOZE_CAP_PER_CYCLE

    monkeypatch.setattr(
        refill_handler.refill_reminders,
        "count_snoozes_for_cycle",
        count_snoozes,
    )
    _patch_session(monkeypatch)

    delta = await refill_handler.handle_refill_action(
        patient_phone="9100",
        new_user_text="[refill-action] snoozed regimen_id=1",
    )
    assert delta is not None
    assert delta["audit_reasons"] == ["refill_action_snooze_cap_hit"]
    # Existing ticket — no new ticket created, no new snooze enqueued.
    assert not any(c[0] == "create_ticket" for c in captured["calls"])
    assert not any(c[0] == "enqueue" for c in captured["calls"])


async def test_handle_help_creates_ops_ticket(monkeypatch):
    captured = _stub_repos(
        monkeypatch, regimen=_regimen(), patient=_patient()
    )
    _patch_session(monkeypatch)

    delta = await refill_handler.handle_refill_action(
        patient_phone="9100",
        new_user_text="[refill-action] help regimen_id=1",
    )
    assert delta is not None
    assert "team" in delta["response_body"].lower()
    assert delta["audit_reasons"] == ["refill_action_help"]
    ticket_calls = [c for c in captured["calls"] if c[0] == "create_ticket"]
    assert len(ticket_calls) == 1
    kwargs = ticket_calls[0][1]
    assert kwargs["category"] == "refill_help"
    assert kwargs["patient_id"] == "9100"


async def test_handle_help_skips_when_open_ticket_exists(monkeypatch):
    captured = _stub_repos(
        monkeypatch,
        regimen=_regimen(),
        patient=_patient(),
        open_help_ticket=types.SimpleNamespace(id=11),
    )
    _patch_session(monkeypatch)

    delta = await refill_handler.handle_refill_action(
        patient_phone="9100",
        new_user_text="[refill-action] help regimen_id=1",
    )
    assert delta is not None
    assert "already" in delta["response_body"].lower()
    assert not any(c[0] == "create_ticket" for c in captured["calls"])


async def test_handle_refuses_cross_patient(monkeypatch):
    captured = _stub_repos(
        monkeypatch,
        regimen=_regimen(patient_id=99),  # owned by patient 99
        patient=_patient(id=2, phone="9100"),  # but inbound from patient 2
    )
    _patch_session(monkeypatch)

    delta = await refill_handler.handle_refill_action(
        patient_phone="9100",
        new_user_text="[refill-action] done regimen_id=1",
    )
    assert delta is not None
    assert "your own account" in delta["response_body"].lower()
    assert delta["audit_reasons"] == ["refill_action_cross_patient_refused"]
    assert not any(c[0] == "reset_supply" for c in captured["calls"])


async def test_handle_returns_none_when_marker_absent(monkeypatch):
    _patch_session(monkeypatch)
    out = await refill_handler.handle_refill_action(
        patient_phone="9100", new_user_text="hi there"
    )
    assert out is None

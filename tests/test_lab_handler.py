"""Unit tests for the orchestrator lab follow-up handler.

Mocks DB session + repos so we cover only the action-routing logic.
"""

from __future__ import annotations

import types


from app.db.models import FollowupStatus
from services.orchestrator import lab_handler


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

    monkeypatch.setattr(lab_handler, "get_sessionmaker", lambda: factory)


def _lab(*, id=1, patient_id=2, status=FollowupStatus.due, due_by=None):
    return types.SimpleNamespace(
        id=id,
        patient_id=patient_id,
        test_name="HbA1c",
        status=status,
        due_by=due_by,
    )


def _patient(id=2, phone="9100"):
    return types.SimpleNamespace(id=id, phone=phone)


def _stub_repos(monkeypatch, *, lab, patient, open_help_ticket=None):
    captured = {"calls": []}

    async def get_lab(_db, _id):
        return lab

    async def get_patient_by_phone(_db, _phone):
        return patient

    async def mark_booked(_db, _id, **kwargs):
        captured["calls"].append(("mark_booked", _id, kwargs))
        return lab

    async def mark_completed(_db, _id, **kwargs):
        captured["calls"].append(("mark_completed", _id, kwargs))
        return lab

    async def cancel_for_lab(_db, **kwargs):
        captured["calls"].append(("cancel_for_lab", kwargs))
        return 0

    async def find_open(_db, *, patient_id, category):
        return open_help_ticket

    async def create_ticket(_db, **kwargs):
        captured["calls"].append(("create_ticket", kwargs))
        return types.SimpleNamespace(id=42, **kwargs)

    monkeypatch.setattr(lab_handler.lab_followups_repo, "get", get_lab)
    monkeypatch.setattr(
        lab_handler.patients_repo, "get_by_phone", get_patient_by_phone
    )
    monkeypatch.setattr(
        lab_handler.lab_followups_repo, "mark_booked", mark_booked
    )
    monkeypatch.setattr(
        lab_handler.lab_followups_repo, "mark_completed", mark_completed
    )
    monkeypatch.setattr(
        lab_handler.lab_followups_scheduler,
        "cancel_for_lab_followup",
        cancel_for_lab,
    )
    monkeypatch.setattr(
        lab_handler.ops_tickets_repo,
        "find_open_for_patient_category",
        find_open,
    )
    monkeypatch.setattr(lab_handler.ops_tickets_repo, "create", create_ticket)
    return captured


def test_looks_like_lab_action_recognises_marker():
    assert lab_handler.looks_like_lab_action(
        "[lab-action] booked lab_followup_id=4"
    )
    assert lab_handler.looks_like_lab_action(
        "[lab-action] completed lab_followup_id=4"
    )
    assert lab_handler.looks_like_lab_action(
        "[lab-action] help lab_followup_id=4"
    )
    assert not lab_handler.looks_like_lab_action("I went to the lab")
    assert not lab_handler.looks_like_lab_action("")


async def test_handle_booked_marks_lab_booked(monkeypatch):
    captured = _stub_repos(
        monkeypatch, lab=_lab(status=FollowupStatus.due), patient=_patient()
    )
    _patch_session(monkeypatch)

    delta = await lab_handler.handle_lab_action(
        patient_phone="9100",
        new_user_text="[lab-action] booked lab_followup_id=1",
    )
    assert delta is not None
    assert "booked" in delta["response_body"].lower()
    assert delta["audit_reasons"] == ["lab_action_booked"]
    actions = [c[0] for c in captured["calls"]]
    assert "mark_booked" in actions


async def test_handle_completed_marks_lab_completed_and_cancels_reminders(monkeypatch):
    captured = _stub_repos(
        monkeypatch, lab=_lab(status=FollowupStatus.booked), patient=_patient()
    )
    _patch_session(monkeypatch)

    delta = await lab_handler.handle_lab_action(
        patient_phone="9100",
        new_user_text="[lab-action] completed lab_followup_id=1",
    )
    assert delta is not None
    assert "completed" in delta["response_body"].lower()
    assert delta["audit_reasons"] == ["lab_action_completed"]
    actions = [c[0] for c in captured["calls"]]
    # Both completion AND cancel-future-reminders should fire.
    assert "mark_completed" in actions
    assert "cancel_for_lab" in actions


async def test_handle_completed_idempotent_when_already_completed(monkeypatch):
    captured = _stub_repos(
        monkeypatch,
        lab=_lab(status=FollowupStatus.completed),
        patient=_patient(),
    )
    _patch_session(monkeypatch)

    delta = await lab_handler.handle_lab_action(
        patient_phone="9100",
        new_user_text="[lab-action] completed lab_followup_id=1",
    )
    assert delta is not None
    assert "already" in delta["response_body"].lower()
    # Neither completion nor cancellation should fire again.
    assert not any(c[0] == "mark_completed" for c in captured["calls"])
    assert not any(c[0] == "cancel_for_lab" for c in captured["calls"])


async def test_handle_help_creates_ops_ticket(monkeypatch):
    captured = _stub_repos(
        monkeypatch, lab=_lab(), patient=_patient()
    )
    _patch_session(monkeypatch)

    delta = await lab_handler.handle_lab_action(
        patient_phone="9100",
        new_user_text="[lab-action] help lab_followup_id=1",
    )
    assert delta is not None
    assert delta["audit_reasons"] == ["lab_action_help"]
    ticket_calls = [c for c in captured["calls"] if c[0] == "create_ticket"]
    assert len(ticket_calls) == 1
    assert ticket_calls[0][1]["category"] == "lab_help"


async def test_handle_help_skips_when_open_ticket_exists(monkeypatch):
    captured = _stub_repos(
        monkeypatch,
        lab=_lab(),
        patient=_patient(),
        open_help_ticket=types.SimpleNamespace(id=11),
    )
    _patch_session(monkeypatch)

    delta = await lab_handler.handle_lab_action(
        patient_phone="9100",
        new_user_text="[lab-action] help lab_followup_id=1",
    )
    assert delta is not None
    assert "already have a lab help ticket" in delta["response_body"].lower()
    assert not any(c[0] == "create_ticket" for c in captured["calls"])


async def test_handle_refuses_cross_patient(monkeypatch):
    captured = _stub_repos(
        monkeypatch,
        lab=_lab(patient_id=99),  # owned by patient 99
        patient=_patient(id=2),  # but inbound from patient 2
    )
    _patch_session(monkeypatch)

    delta = await lab_handler.handle_lab_action(
        patient_phone="9100",
        new_user_text="[lab-action] booked lab_followup_id=1",
    )
    assert delta is not None
    assert "your own account" in delta["response_body"].lower()
    assert delta["audit_reasons"] == ["lab_action_cross_patient_refused"]
    assert not any(c[0] == "mark_booked" for c in captured["calls"])


async def test_handle_returns_none_when_marker_absent(monkeypatch):
    _patch_session(monkeypatch)
    out = await lab_handler.handle_lab_action(
        patient_phone="9100", new_user_text="hello"
    )
    assert out is None

"""Unit tests for the recap quick-reply handler and the deterministic
recap renderer. DB and ops_tickets repo are stubbed so we cover only
the action-routing logic.
"""

from __future__ import annotations

import types

import pytest

from app.db.models import RecapStatus
from services.orchestrator import recap_handler
from services.orchestrator.recap_generator import (
    RecapContext,
    render_deterministic,
)


class _NoopAsyncSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None

    async def commit(self):
        return None


def _patch_session(monkeypatch):
    monkeypatch.setattr(
        recap_handler, "get_sessionmaker", lambda: lambda: _NoopAsyncSession()
    )


def _recap(*, id=1, patient_id=2, status=RecapStatus.sent):
    return types.SimpleNamespace(
        id=id, patient_id=patient_id, status=status
    )


def _patient(id=2, phone="9100"):
    return types.SimpleNamespace(id=id, phone=phone)


def _stub_repos(
    monkeypatch,
    *,
    recap=None,
    latest=None,
    patient=None,
    open_question_ticket=None,
):
    captured = {"calls": []}

    async def get_recap(_db, recap_id):
        return recap

    async def find_latest(_db, _patient_id):
        return latest

    async def get_patient_by_phone(_db, _phone):
        return patient

    async def mark_acknowledged(_db, recap_id):
        captured["calls"].append(("mark_acknowledged", recap_id))
        return recap or latest

    async def mark_questioned(_db, recap_id):
        captured["calls"].append(("mark_questioned", recap_id))
        return recap or latest

    async def find_open(_db, *, patient_id, category):
        return open_question_ticket

    async def create_ticket(_db, **kwargs):
        captured["calls"].append(("create_ticket", kwargs))
        return types.SimpleNamespace(id=42, **kwargs)

    monkeypatch.setattr(
        recap_handler.appointment_recaps_repo, "get", get_recap
    )
    monkeypatch.setattr(
        recap_handler.appointment_recaps_repo,
        "find_latest_sent_for_patient",
        find_latest,
    )
    monkeypatch.setattr(
        recap_handler.appointment_recaps_repo,
        "mark_acknowledged",
        mark_acknowledged,
    )
    monkeypatch.setattr(
        recap_handler.appointment_recaps_repo,
        "mark_questioned",
        mark_questioned,
    )
    monkeypatch.setattr(
        recap_handler.patients_repo, "get_by_phone", get_patient_by_phone
    )
    monkeypatch.setattr(
        recap_handler.ops_tickets_repo,
        "find_open_for_patient_category",
        find_open,
    )
    monkeypatch.setattr(recap_handler.ops_tickets_repo, "create", create_ticket)
    return captured


def test_marker_recogniser_matches_both_actions():
    assert recap_handler.looks_like_recap_action(
        "[recap-action] ack recap_id=1"
    )
    assert recap_handler.looks_like_recap_action(
        "[recap-action] question recap_id=12"
    )
    # Plain text variants are also recognised.
    assert recap_handler.looks_like_recap_action("OK")
    assert recap_handler.looks_like_recap_action("ok")
    assert recap_handler.looks_like_recap_action("Got it")
    assert recap_handler.looks_like_recap_action("Thanks")
    assert recap_handler.looks_like_recap_action("I have a question")
    assert recap_handler.looks_like_recap_action(
        "I have a question about the medication"
    )
    # Unrelated content should not match.
    assert not recap_handler.looks_like_recap_action(
        "I'm running out of metformin"
    )
    assert not recap_handler.looks_like_recap_action("")


async def test_marker_ack_marks_recap_acknowledged(monkeypatch):
    captured = _stub_repos(
        monkeypatch, recap=_recap(), patient=_patient()
    )
    _patch_session(monkeypatch)

    delta = await recap_handler.handle_recap_action(
        patient_phone="9100",
        new_user_text="[recap-action] ack recap_id=1",
    )
    assert delta is not None
    assert delta["audit_reasons"] == ["recap_action_ack"]
    assert any(c[0] == "mark_acknowledged" for c in captured["calls"])


async def test_plain_ok_resolves_via_latest_recap(monkeypatch):
    captured = _stub_repos(
        monkeypatch,
        latest=_recap(id=7),
        patient=_patient(),
    )
    _patch_session(monkeypatch)

    delta = await recap_handler.handle_recap_action(
        patient_phone="9100", new_user_text="OK"
    )
    assert delta is not None
    assert delta["audit_reasons"] == ["recap_action_ack"]
    # Should have used the recap_id from the latest lookup, not a marker id.
    ack_calls = [c for c in captured["calls"] if c[0] == "mark_acknowledged"]
    assert ack_calls and ack_calls[0][1] == 7


async def test_plain_ok_with_no_recent_recap_returns_none(monkeypatch):
    """Patient says 'OK' but has no pending recap — handler returns
    None so the inbound flows on to the LLM/intent path."""
    _stub_repos(monkeypatch, latest=None, patient=_patient())
    _patch_session(monkeypatch)

    delta = await recap_handler.handle_recap_action(
        patient_phone="9100", new_user_text="OK"
    )
    assert delta is None


async def test_question_marks_recap_and_creates_ticket(monkeypatch):
    captured = _stub_repos(
        monkeypatch, recap=_recap(), patient=_patient()
    )
    _patch_session(monkeypatch)

    delta = await recap_handler.handle_recap_action(
        patient_phone="9100",
        new_user_text="[recap-action] question recap_id=1",
    )
    assert delta is not None
    assert delta["audit_reasons"] == ["recap_action_question"]
    assert any(c[0] == "mark_questioned" for c in captured["calls"])
    ticket_calls = [c for c in captured["calls"] if c[0] == "create_ticket"]
    assert len(ticket_calls) == 1
    assert ticket_calls[0][1]["category"] == "recap_question"
    assert ticket_calls[0][1]["sla_minutes"] == 1440


async def test_question_idempotent_when_open_ticket_exists(monkeypatch):
    captured = _stub_repos(
        monkeypatch,
        latest=_recap(),
        patient=_patient(),
        open_question_ticket=types.SimpleNamespace(id=99),
    )
    _patch_session(monkeypatch)

    delta = await recap_handler.handle_recap_action(
        patient_phone="9100",
        new_user_text="I have a question",
    )
    assert delta is not None
    # Recap still gets flagged as questioned, but no second ticket is created.
    assert any(c[0] == "mark_questioned" for c in captured["calls"])
    assert not any(c[0] == "create_ticket" for c in captured["calls"])


async def test_cross_patient_refused(monkeypatch):
    captured = _stub_repos(
        monkeypatch,
        recap=_recap(patient_id=99),  # owned by patient 99
        patient=_patient(id=2),  # but inbound from patient 2
    )
    _patch_session(monkeypatch)

    delta = await recap_handler.handle_recap_action(
        patient_phone="9100",
        new_user_text="[recap-action] ack recap_id=1",
    )
    assert delta is not None
    assert delta["audit_reasons"] == ["recap_action_cross_patient_refused"]
    # No state mutation on either recap or ticket repos.
    assert not any(
        c[0] in {"mark_acknowledged", "mark_questioned", "create_ticket"}
        for c in captured["calls"]
    )


# ---- Deterministic generator tests --------------------------------------


def test_render_deterministic_includes_all_sections():
    body = render_deterministic(
        RecapContext(
            patient_first_name="Sam",
            doctor_name="Dr. Lee",
            appointment_date_local="Mon 4 May, 11:00 AM",
            doctor_notes="Continue current plan.",
            meds_added=[
                {"name": "Vitamin D3", "instructions": "1 daily"}
            ],
            meds_changed=[
                {"name": "Metformin", "change": "increase to 500mg twice daily"}
            ],
            meds_stopped=[{"name": "Old beta-blocker"}],
            labs_ordered=[{"test_name": "HbA1c"}],
            next_followup_in_days=90,
            red_flags=["chest pain", "blood sugar below 70"],
        )
    )
    # Greeting + doctor + date.
    assert "Hi Sam," in body
    assert "Dr. Lee" in body
    assert "Mon 4 May" in body
    # Doctor notes carry through.
    assert "Continue current plan." in body
    # Each med renders on its own bullet with markers.
    assert "• Vitamin D3 — 1 daily (new)" in body
    assert "• Metformin — increase to 500mg twice daily (updated)" in body
    assert "• Stop taking Old beta-blocker" in body
    # Labs + next visit.
    assert "• HbA1c" in body
    assert "Next visit:" in body
    # Red-flag prefix mandated by the prompt contract.
    assert "Call us right away if:" in body
    assert "• chest pain" in body
    # Sign-off / call-to-action present.
    assert "Reply OK to acknowledge" in body
    # Hard length cap respected.
    assert len(body) <= 900


def test_render_deterministic_skips_empty_sections():
    body = render_deterministic(
        RecapContext(
            patient_first_name=None,
            doctor_name="Dr. Lee",
            appointment_date_local="Mon 4 May, 11:00 AM",
        )
    )
    assert "Hi," in body
    assert "Medications:" not in body
    assert "Tests to do:" not in body
    assert "Call us right away if:" not in body
    # Always closes with the ack/question prompt.
    assert "Reply OK to acknowledge" in body


def test_render_deterministic_next_visit_phrasing():
    """1 day → tomorrow; <14 days → in N days; <60 → weeks; else months."""

    def body_for(days: int) -> str:
        return render_deterministic(
            RecapContext(
                doctor_name="Dr. Lee",
                appointment_date_local="x",
                next_followup_in_days=days,
            )
        )

    assert "tomorrow" in body_for(1)
    assert "in 5 days" in body_for(5)
    assert "weeks" in body_for(28)
    assert "months" in body_for(180)

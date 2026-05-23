"""Unit tests for the patient self-service lookup handler.

Repos are stubbed at the handler's import boundary. We test:

    - Classifier discipline (positives, negatives, ambiguity).
      The matchers are anchored start-and-end; false positives
      would short-circuit a different question into a structured
      response that doesn't match it, which is worse than missing
      a query (the LLM compose path handles misses gracefully).
    - Renderer output for both languages we ship copy in (English
      + Hindi) plus the unknown-language English fallback.
    - Empty-state copy (patient has no active regimens / no
      pending labs).
    - The two-query handler dispatch + lookup_no_patient defensive
      fall-through.
"""

from __future__ import annotations

import types
from datetime import date

import pytest

from app.db.models import FollowupStatus
from services.orchestrator import lookup_handler as h


# ---- Classifier ----------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "what meds am I on",
        "what medications am I taking",
        "show my meds",
        "list my medications",
        "tell me my prescriptions",
        "what am I taking",
        "what do I take",
        "do I have any medications",
        "my meds",
        "my medications",
        "current meds",
        "active medications",
        "What meds am I on?",
        "What medicines am I taking?",
        "what drugs do I take",
        "  show my meds  ",  # whitespace tolerated
        "List my prescriptions.",
    ],
)
def test_classifier_medications_positive(text):
    assert h.classify_lookup_query(text) == "medications"


@pytest.mark.parametrize(
    "text",
    [
        "what labs do I have",
        "what tests are due",
        "show my labs",
        "list my tests",
        "do I have any blood tests",
        "my labs",
        "my tests",
        "upcoming tests",
        "pending labs",
        "labs due",
        "tests due",
        "what is my blood work",
        "what's my blood work",
        "outstanding tests",
    ],
)
def test_classifier_labs_positive(text):
    assert h.classify_lookup_query(text) == "labs"


@pytest.mark.parametrize(
    "text",
    [
        # Generic mentions — must NOT route to lookup; let LLM handle.
        "I forgot to take my medication this morning",
        "I think the meds are causing side effects",
        "I need to start taking my meds again",
        "my labs are at apollo clinic",
        # Different intent shapes
        "thanks for the reminder",
        "I have a question",
        "when is my next appointment",
        "cancel my appointment",
        "book an appointment",
        # Edge cases
        "",
        "   ",
        None,
        # Avoid cross-classification
        "my appointments",
    ],
)
def test_classifier_negative(text):
    assert h.classify_lookup_query(text) is None


# ---- _render localisation ------------------------------------------------


def test_render_english_default():
    assert "active medications" in h._render("meds_header", "en")
    assert "lab tests" in h._render("labs_header", "en")


def test_render_hindi_translates():
    # Devanagari "मौजूदा दवाइयाँ" leads the Hindi meds header.
    assert "मौजूदा दवाइयाँ" in h._render("meds_header", "hi")
    assert "लैब टेस्ट" in h._render("labs_header", "hi")


def test_render_unknown_language_falls_back_to_english():
    en = h._render("meds_header", "en")
    assert h._render("meds_header", "ta") == en
    assert h._render("meds_header", "xx") == en
    assert h._render("meds_header", None) == en


# ---- Renderers -----------------------------------------------------------


def _regimen(name="Metformin", dose="500 mg", id=1):
    return types.SimpleNamespace(
        id=id,
        medication_name=name,
        dose=dose,
        schedule={},
        starts_on=None,
        ends_on=None,
    )


def _lab(
    test_name="HbA1c",
    status=FollowupStatus.due,
    due_by=None,
    id=1,
):
    return types.SimpleNamespace(
        id=id,
        test_name=test_name,
        status=status,
        due_by=due_by,
    )


def test_render_medications_lists_active_regimens():
    out = h._render_medications(
        [
            _regimen(name="Metformin", dose="500 mg"),
            _regimen(name="Atorvastatin", dose="10 mg"),
        ],
        "en",
    )
    assert "active medications" in out
    assert "• Metformin 500 mg" in out
    assert "• Atorvastatin 10 mg" in out


def test_render_medications_handles_no_dose_gracefully():
    out = h._render_medications(
        [_regimen(name="Metformin", dose="")], "en"
    )
    # No trailing space when dose is empty.
    assert "• Metformin" in out
    assert "• Metformin " not in out


def test_render_medications_empty_uses_localised_empty_state():
    en = h._render_medications([], "en")
    hi = h._render_medications([], "hi")
    assert "active medications" in en
    # English copy should NOT include the Hindi script.
    assert "मौजूदा" not in en
    assert "सक्रिय दवा" in hi


def test_render_labs_lists_pending_only():
    """Labs in completed/reviewed status are excluded — patients
    asking "what tests do I have" want the actionable subset."""
    out = h._render_labs(
        [
            _lab(test_name="HbA1c", status=FollowupStatus.due, due_by=date(2026, 6, 1)),
            _lab(test_name="Lipid panel", status=FollowupStatus.completed),
            _lab(test_name="LFT", status=FollowupStatus.booked, due_by=date(2026, 5, 15)),
            _lab(test_name="Old test", status=FollowupStatus.reviewed),
        ],
        "en",
    )
    assert "lab tests" in out
    assert "HbA1c" in out
    assert "LFT" in out
    # Completed / reviewed must NOT appear.
    assert "Lipid panel" not in out
    assert "Old test" not in out


def test_render_labs_sorts_due_by_then_no_due():
    """Earliest due_by first; labs without a due_by sort last so the
    most actionable items lead the list."""
    out = h._render_labs(
        [
            _lab(test_name="No date", status=FollowupStatus.due, due_by=None),
            _lab(test_name="Late", status=FollowupStatus.due, due_by=date(2026, 7, 1)),
            _lab(test_name="Early", status=FollowupStatus.due, due_by=date(2026, 6, 1)),
        ],
        "en",
    )
    early_idx = out.find("Early")
    late_idx = out.find("Late")
    no_date_idx = out.find("No date")
    assert 0 < early_idx < late_idx < no_date_idx


def test_render_labs_localised_status_label():
    """Hindi rendering localises both the header AND the per-row
    status (``due`` → ``बाक़ी``)."""
    out = h._render_labs(
        [_lab(test_name="HbA1c", status=FollowupStatus.due, due_by=date(2026, 6, 1))],
        "hi",
    )
    assert "लैब टेस्ट" in out
    assert "बाक़ी" in out
    assert "HbA1c" in out


def test_render_labs_includes_due_by_iso_date():
    """The patient sees the due date as YYYY-MM-DD — unambiguous
    across locales. Date formatting is explicitly NOT localised."""
    out = h._render_labs(
        [_lab(test_name="HbA1c", status=FollowupStatus.due, due_by=date(2026, 6, 15))],
        "en",
    )
    assert "2026-06-15" in out


def test_render_labs_no_pending_uses_empty_state():
    en = h._render_labs(
        [_lab(test_name="X", status=FollowupStatus.completed)], "en"
    )
    # No actionable labs → empty-state copy renders.
    assert "any pending lab tests" in en.lower()
    # Completed labs must not bleed into the response.
    assert "X" not in en


# ---- Handler ------------------------------------------------------------


class _NoopAsyncSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None

    async def commit(self):
        return None


def _patient(*, id=1, phone="9100", preferred_language="en"):
    return types.SimpleNamespace(
        id=id,
        phone=phone,
        full_name="Patient 9100",
        preferred_language=preferred_language,
    )


def _patch(monkeypatch, *, patient, regimens=None, labs=None):
    def factory():
        return _NoopAsyncSession()

    monkeypatch.setattr(h, "get_sessionmaker", lambda: factory)

    async def get_by_phone(_db, _phone):
        return patient

    async def list_regimens_for_patient(_db, _id, *, active_on=None):
        return regimens or []

    async def list_labs_for_patient(_db, _id, *, limit=50):
        return labs or []

    monkeypatch.setattr(h.patients_repo, "get_by_phone", get_by_phone)
    monkeypatch.setattr(
        h.regimens_repo, "list_for_patient", list_regimens_for_patient
    )
    monkeypatch.setattr(
        h.lab_followups_repo, "list_for_patient", list_labs_for_patient
    )


async def test_handle_lookup_medications_renders_active_list(monkeypatch):
    p = _patient()
    _patch(
        monkeypatch,
        patient=p,
        regimens=[_regimen(name="Metformin", dose="500 mg")],
    )
    delta = await h.handle_lookup_query(
        patient_phone="9100", query_type="medications"
    )
    assert delta is not None
    assert delta["audit_reasons"] == ["lookup_medications"]
    assert "Metformin" in delta["response_body"]


async def test_handle_lookup_medications_empty_uses_empty_state(monkeypatch):
    p = _patient()
    _patch(monkeypatch, patient=p, regimens=[])
    delta = await h.handle_lookup_query(
        patient_phone="9100", query_type="medications"
    )
    assert delta["audit_reasons"] == ["lookup_medications"]
    assert "active medications" in delta["response_body"].lower()


async def test_handle_lookup_labs_renders_pending(monkeypatch):
    p = _patient()
    _patch(
        monkeypatch,
        patient=p,
        labs=[
            _lab(test_name="HbA1c", status=FollowupStatus.due, due_by=date(2026, 6, 1)),
        ],
    )
    delta = await h.handle_lookup_query(
        patient_phone="9100", query_type="labs"
    )
    assert delta["audit_reasons"] == ["lookup_labs"]
    assert "HbA1c" in delta["response_body"]
    assert "2026-06-01" in delta["response_body"]


async def test_handle_lookup_renders_in_hindi_when_preferred(monkeypatch):
    """A patient flagged ``preferred_language='hi'`` should see Hindi
    headers + status labels in their lookup response, with the medical
    nouns (which we don't translate) preserved verbatim."""
    p = _patient(preferred_language="hi")
    _patch(
        monkeypatch,
        patient=p,
        regimens=[_regimen(name="Metformin", dose="500 mg")],
    )
    delta = await h.handle_lookup_query(
        patient_phone="9100", query_type="medications"
    )
    assert "मौजूदा दवाइयाँ" in delta["response_body"]
    # Medication names stay in Latin/English — clinical accuracy.
    assert "Metformin 500 mg" in delta["response_body"]


async def test_handle_lookup_missing_patient_returns_no_patient_reply(monkeypatch):
    """Defensive — upsert_patient runs upstream, but if it didn't,
    return a friendly "set up first" message rather than crashing."""

    def factory():
        return _NoopAsyncSession()

    monkeypatch.setattr(h, "get_sessionmaker", lambda: factory)

    async def get_by_phone(_db, _phone):
        return None

    monkeypatch.setattr(h.patients_repo, "get_by_phone", get_by_phone)

    delta = await h.handle_lookup_query(
        patient_phone="9100", query_type="medications"
    )
    assert delta is not None
    assert delta["audit_reasons"] == ["lookup_no_patient"]
    assert "couldn't find your profile" in delta["response_body"].lower()

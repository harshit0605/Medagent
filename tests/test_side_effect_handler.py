"""Unit tests for the side-effect / adverse-reaction inbound handler.

Repos are stubbed at the module's import boundary — we cover only
the matcher discipline + the handler's contract (open ticket, ack
with emergency guidance, capture regimens for context). Integration
coverage runs end-to-end against the real ops_tickets table.

False-positive discipline matters here more than usual. A
mis-routed message would open a high-priority ops ticket on a
non-event ("I had side effects on my software release") which is
worse than a false negative (a real report falls through to the
LLM compose path which still produces a sensible reply). The
matcher is intentionally conservative.
"""

from __future__ import annotations

import types
from datetime import date

import pytest

from services.orchestrator import side_effect_handler as h


# ---- Matcher: positives ---------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # Pattern 1: literal "side effect(s)" phrase.
        "I have a side effect from this medication",
        "These pills are giving me side effects",
        "side effects include nausea",
        "side-effect alert",
        "This is a side-effect",
        # Pattern 2: allergic-reaction language.
        "I am having a bad reaction to my meds",
        "I think I am having an allergic reaction",
        "allergic reaction starting",
        "having an adverse reaction",
        "I am allergic to this medication",
        "allergic to my medication",
        # Pattern 3: symptom-then-attribution (med-attributed).
        "I am dizzy from the medication",
        "rash from taking the meds",
        "nausea since starting my prescription",
        "vomiting after taking the pills",
        "breathing problems from the medication",
        "headaches from this prescription",
        # Pattern 4: med-as-subject causation.
        "this medication is making me dizzy",
        "the meds are making me sick",
        "these pills are giving me a rash",
        "this medication has been making me drowsy",
        "the meds are causing me nausea",
        "my pills are causing me nausea",
        # Pattern 5: named-drug causation.
        "metformin gave me headaches",
        "atorvastatin is causing me nausea",
        "the new prescription caused vomiting",
        # Pattern 6: Hindi "साइड इफेक्ट" loanword form.
        "साइड इफेक्ट हो रहा है",
        "मुझे साइड-इफेक्ट हो रहे हैं",
        # Mixed case (Pattern 1 must be case-insensitive).
        "SIDE EFFECTS",
    ],
)
def test_matcher_positive(text):
    assert h.looks_like_side_effect_report(text)


# ---- Matcher: negatives ---------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # Generic conversation
        "thanks for the reminder",
        "I have a question",
        "when is my next appointment",
        # Non-medical "side effects" usage.
        "this is a project with side benefits",
        # Generic symptom mentions WITHOUT med attribution — must
        # NOT trigger. The LLM compose path can still respond
        # appropriately.
        "my daughter has been dizzy lately",
        "I am tired today",
        "I have a headache",
        "I had a nausea",
        # Allergy disclosure (not medication-related).
        "allergic to peanuts",
        "I am allergic to dust",
        # Med-related but NOT adverse.
        "I am taking my medication",
        "I forgot to take my medication this morning",
        "I think the new pills are working great",
        # Different intent shapes
        "reschedule appointment",
        "cancel my booking",
        "what meds am I on",
        # Edge cases
        "",
        "   ",
        None,
    ],
)
def test_matcher_negative(text):
    assert not h.looks_like_side_effect_report(text)


# ---- Localised acks -------------------------------------------------------


def test_render_english_ack_includes_emergency_guidance():
    """The ack MUST include emergency-services guidance — patients
    reporting acute symptoms need a clear path to the ER, not just
    a "thanks we'll review" message."""
    out = h._render("ack", "en")
    assert "112" in out  # India emergency number
    assert "emergency" in out.lower()
    assert "care team" in out.lower()


def test_render_hindi_ack_translates():
    out = h._render("ack", "hi")
    # Devanagari "धन्यवाद" leads the Hindi ack.
    assert "धन्यवाद" in out
    # Same emergency-number hint, in Hindi context.
    assert "112" in out
    # Critical Devanagari terms — confirms we picked up the Hindi
    # entry and aren't silently falling back to English.
    assert "केयर टीम" in out


def test_render_unknown_language_falls_back_to_english():
    en = h._render("ack", "en")
    assert h._render("ack", "ta") == en
    assert h._render("ack", None) == en
    assert h._render("ack", "xx") == en


# ---- _build_ticket_notes -------------------------------------------------


def _regimen(name="Metformin", dose="500 mg", id=1, starts_on=None):
    return types.SimpleNamespace(
        id=id,
        medication_name=name,
        dose=dose,
        starts_on=starts_on,
    )


def test_ticket_notes_include_verbatim_inbound():
    """The clinician needs to see what the PATIENT actually said,
    not a paraphrase. Verbatim inbound, prefixed for scannability."""
    notes = h._build_ticket_notes(
        inbound="I have a rash from the medication",
        regimens=[],
    )
    assert "side-effect report" in notes
    assert "I have a rash from the medication" in notes


def test_ticket_notes_include_active_regimens():
    """Regimens are captured so the doctor doesn't have to look
    them up. Without this, the ticket reads "patient reports
    nausea" with no list of medications to cross-reference."""
    notes = h._build_ticket_notes(
        inbound="nausea from the pills",
        regimens=[
            _regimen(name="Metformin", dose="500 mg", starts_on=date(2026, 1, 1)),
            _regimen(name="Atorvastatin", dose="10 mg"),
        ],
    )
    assert "Metformin 500 mg" in notes
    assert "started 2026-01-01" in notes
    assert "Atorvastatin 10 mg" in notes


def test_ticket_notes_no_regimens_renders_explicit_message():
    """Empty regimens render an explicit ``(none on file)`` so the
    clinician knows the absence is real, not a missing data fetch."""
    notes = h._build_ticket_notes(inbound="something", regimens=[])
    assert "(none on file)" in notes


def test_ticket_notes_handle_empty_inbound_gracefully():
    """Empty inbound shouldn't crash the renderer or leave a
    trailing blank quote."""
    notes = h._build_ticket_notes(inbound="", regimens=[])
    assert "(empty inbound)" in notes


# ---- handle_side_effect_report -------------------------------------------


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


def _patch(monkeypatch, *, patient, regimens=None, ticket_id=999):
    captured = {"creates": [], "regimen_lookups": []}

    def factory():
        return _NoopAsyncSession()

    monkeypatch.setattr(h, "get_sessionmaker", lambda: factory)

    async def get_by_phone(_db, _phone):
        return patient

    async def list_regimens_for_patient(_db, _id, *, active_on=None):
        captured["regimen_lookups"].append((_id, active_on))
        return regimens or []

    async def create_ticket(
        _db,
        *,
        patient_id,
        category,
        priority,
        sla_minutes,
        notes=None,
    ):
        ticket = types.SimpleNamespace(
            id=ticket_id,
            patient_id=patient_id,
            category=category,
            priority=priority,
            sla_minutes=sla_minutes,
            notes=notes,
        )
        captured["creates"].append(ticket)
        return ticket

    monkeypatch.setattr(h.patients_repo, "get_by_phone", get_by_phone)
    monkeypatch.setattr(
        h.regimens_repo, "list_for_patient", list_regimens_for_patient
    )
    monkeypatch.setattr(h.ops_tickets_repo, "create", create_ticket)
    return captured


async def test_handle_opens_high_priority_ticket(monkeypatch):
    """Happy path — patient reports a side effect, we open a ticket
    with the right category / priority / SLA + verbatim inbound +
    regimen context."""
    p = _patient()
    captured = _patch(
        monkeypatch,
        patient=p,
        regimens=[_regimen(name="Metformin", dose="500 mg")],
    )

    delta = await h.handle_side_effect_report(
        patient_phone="9100",
        new_user_text="metformin gave me severe headaches",
    )
    assert delta is not None
    assert delta["audit_reasons"] == ["side_effect_logged"]
    # Ticket created with the documented contract.
    assert len(captured["creates"]) == 1
    ticket = captured["creates"][0]
    assert ticket.category == h.CATEGORY
    assert ticket.priority == h.PRIORITY
    assert ticket.sla_minutes == h.SLA_MINUTES
    # Notes carry the verbatim inbound + regimen context.
    assert "metformin gave me severe headaches" in ticket.notes
    assert "Metformin 500 mg" in ticket.notes


async def test_handle_uses_patient_phone_not_db_id_for_ticket(monkeypatch):
    """ops_tickets.patient_id is the WhatsApp phone (string), NOT
    the patients.id integer. The handler must pass patient.phone so
    the ticket joins back to the patient via the established
    convention."""
    p = _patient(id=42, phone="+919812345678")
    captured = _patch(monkeypatch, patient=p)

    await h.handle_side_effect_report(
        patient_phone="+919812345678",
        new_user_text="side effects",
    )
    assert captured["creates"][0].patient_id == "+919812345678"


async def test_handle_renders_ack_in_hindi_when_preferred(monkeypatch):
    """A Hindi-preferring patient reporting a side effect must see
    the Hindi ack. The emergency guidance is critical and must
    survive the localisation."""
    p = _patient(preferred_language="hi")
    _patch(monkeypatch, patient=p)

    delta = await h.handle_side_effect_report(
        patient_phone="9100",
        new_user_text="साइड इफेक्ट हो रहा है",
    )
    assert "धन्यवाद" in delta["response_body"]
    assert "112" in delta["response_body"]


async def test_handle_sets_escalation_required(monkeypatch):
    """The reply delta must mark ``escalation_required=True`` so
    downstream surfaces (audit log, doctor inbox) treat this as
    high-priority. Otherwise the side-effect report would render
    alongside routine queries."""
    p = _patient()
    _patch(monkeypatch, patient=p)

    delta = await h.handle_side_effect_report(
        patient_phone="9100", new_user_text="side effects"
    )
    assert delta["escalation_required"] is True
    assert delta["risk_level"] == "high"


async def test_handle_continues_when_regimen_lookup_fails(monkeypatch):
    """A transient DB error fetching regimens must NOT block the
    ticket open — the report itself is what matters. Ticket gets
    created with empty regimen context; ops can still triage."""
    p = _patient()
    captured = {"creates": []}

    def factory():
        return _NoopAsyncSession()

    monkeypatch.setattr(h, "get_sessionmaker", lambda: factory)

    async def get_by_phone(_db, _phone):
        return p

    async def list_regimens_for_patient(_db, _id, *, active_on=None):
        raise RuntimeError("simulated transient failure")

    async def create_ticket(
        _db,
        *,
        patient_id,
        category,
        priority,
        sla_minutes,
        notes=None,
    ):
        ticket = types.SimpleNamespace(
            id=1,
            patient_id=patient_id,
            category=category,
            notes=notes,
        )
        captured["creates"].append(ticket)
        return ticket

    monkeypatch.setattr(h.patients_repo, "get_by_phone", get_by_phone)
    monkeypatch.setattr(
        h.regimens_repo, "list_for_patient", list_regimens_for_patient
    )
    monkeypatch.setattr(h.ops_tickets_repo, "create", create_ticket)

    delta = await h.handle_side_effect_report(
        patient_phone="9100", new_user_text="side effects"
    )
    assert delta is not None
    assert len(captured["creates"]) == 1
    # Notes carry the explicit "(none on file)" sentinel.
    assert "(none on file)" in captured["creates"][0].notes


async def test_handle_missing_patient_returns_no_patient_reply(monkeypatch):
    """Defensive — upsert_patient runs upstream, but if it didn't,
    return a helpful "set up first" message rather than crashing."""

    def factory():
        return _NoopAsyncSession()

    monkeypatch.setattr(h, "get_sessionmaker", lambda: factory)

    async def get_by_phone(_db, _phone):
        return None

    monkeypatch.setattr(h.patients_repo, "get_by_phone", get_by_phone)

    delta = await h.handle_side_effect_report(
        patient_phone="9100", new_user_text="side effects"
    )
    assert delta is not None
    assert delta["audit_reasons"] == ["side_effect_no_patient"]
    assert "noted" in delta["response_body"].lower()

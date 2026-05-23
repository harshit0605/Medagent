"""Unit tests for the STOP / START keyword opt-out handler.

DB is mocked at the repo boundary so we cover only the matcher
discipline + the handler's state-mutation contract.

The matchers are intentionally STRICT-anchored — a patient saying
"I'll stop the medication" must NOT trigger opt-out. False positives
revoke consent silently and are far worse than false negatives.
"""

from __future__ import annotations

import types
from datetime import datetime, timezone

import pytest

from services.orchestrator import optout_handler as h


# ---- Matcher: STOP-family --------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "STOP",
        "stop",
        "Stop",
        "STOP.",
        "stop please",
        "Stop now",
        "stop messages",
        "stop sending",
        "unsubscribe",
        "UNSUBSCRIBE",
        "unsub",
        "opt out",
        "opt-out",
        "OPTOUT",
        "leave me alone",
        "stop messaging me",
        "cancel reminders",
        "cancel reminder",
        "cancel messages",
        "cancel subscription",
        "disable reminders",
        "disable messages",
        "  stop  ",  # leading/trailing whitespace tolerated
        "stop!",
    ],
)
def test_looks_like_optout_positive(text):
    assert h.looks_like_optout(text)


@pytest.mark.parametrize(
    "text",
    [
        "I'll stop the medication",
        "I want to stop",
        "stop being silly",
        "please don't stop",
        "I need to unsubscribe from netflix tomorrow",
        "cancel my appointment",
        "cancel the order",
        "i hate when people stop replying",
        "leave me alone with my homework",  # too many trailing words
        "subscribe me",
        "",
        None,
        "STOP STOP STOP",  # repeated keyword shouldn't slip through
    ],
)
def test_looks_like_optout_negative(text):
    assert not h.looks_like_optout(text)


# ---- Matcher: START-family -------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "START",
        "start",
        "Start.",
        "start reminders",
        "start messaging",
        "subscribe",
        "opt in",
        "opt-in",
        "OPTIN",
        "enable reminders",
        "resume reminders",
        "resume messages",
        "begin reminders",
    ],
)
def test_looks_like_optin_positive(text):
    assert h.looks_like_optin(text)


@pytest.mark.parametrize(
    "text",
    [
        "I want to start running",
        "start the medication",
        "I need to subscribe to a magazine",
        "opt me in to your bonus program",  # trailing words → no match
        "",
        None,
        "stop",  # mustn't cross-match the OPTIN matcher
    ],
)
def test_looks_like_optin_negative(text):
    assert not h.looks_like_optin(text)


# ---- _render localisation --------------------------------------------------


def test_render_english_default():
    assert "opted out" in h._render("optout_ack", "en")


def test_render_hindi_translates():
    # ``समझ गया`` is the Hindi leading phrase of the optout_ack copy.
    assert "समझ गया" in h._render("optout_ack", "hi")
    # ``ऑप्ट-आउट`` (transliterated "opt-out") shows up in the
    # already-opted-out variant.
    assert "ऑप्ट-आउट" in h._render("optout_already", "hi")


def test_render_unknown_language_falls_back_to_english():
    assert h._render("optout_ack", "ta") == h._render("optout_ack", "en")
    assert h._render("optout_ack", None) == h._render("optout_ack", "en")
    assert h._render("optout_ack", "xx") == h._render("optout_ack", "en")


# ---- handle_optout / handle_optin ----------------------------------------


class _NoopAsyncSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None

    async def commit(self):
        return None


def _patient(
    *,
    id=1,
    phone="9100",
    consent_sms=True,
    consent_revoked_at=None,
    consent_revoked_reason=None,
    preferred_language="en",
):
    return types.SimpleNamespace(
        id=id,
        phone=phone,
        full_name="Patient 9100",
        consent_sms=consent_sms,
        consent_revoked_at=consent_revoked_at,
        consent_revoked_reason=consent_revoked_reason,
        preferred_language=preferred_language,
    )


def _patch(monkeypatch, *, patient):
    captured = {"revokes": [], "restores": []}

    def factory():
        return _NoopAsyncSession()

    monkeypatch.setattr(h, "get_sessionmaker", lambda: factory)

    async def get_by_phone(_db, _phone):
        return patient

    async def revoke_consent(_db, _id, *, reason="patient_stop_keyword", when=None):
        captured["revokes"].append((_id, reason))
        patient.consent_sms = False
        patient.consent_revoked_at = when or datetime.now(timezone.utc)
        patient.consent_revoked_reason = reason
        return patient

    async def restore_consent(_db, _id):
        captured["restores"].append(_id)
        patient.consent_sms = True
        patient.consent_revoked_at = None
        patient.consent_revoked_reason = None
        return patient

    monkeypatch.setattr(h.patients_repo, "get_by_phone", get_by_phone)
    monkeypatch.setattr(h.patients_repo, "revoke_consent", revoke_consent)
    monkeypatch.setattr(h.patients_repo, "restore_consent", restore_consent)
    return captured


async def test_handle_optout_revokes_consent_and_acks(monkeypatch):
    p = _patient(consent_sms=True)
    captured = _patch(monkeypatch, patient=p)

    delta = await h.handle_optout(
        patient_phone="9100", new_user_text="STOP"
    )
    assert delta is not None
    assert delta["audit_reasons"] == ["optout"]
    assert "opted out" in delta["response_body"].lower()
    # Repo helper called with the canonical reason string.
    assert captured["revokes"] == [(p.id, "patient_stop_keyword")]
    assert p.consent_sms is False
    assert p.consent_revoked_at is not None


async def test_handle_optout_already_revoked_just_acks_no_double_stamp(monkeypatch):
    """A patient who's already opted out shouldn't see their
    revoked_at timestamp overwritten by a second STOP — the original
    moment is what ops cares about. The ack still goes out so the
    patient sees confirmation."""
    earlier = datetime(2026, 1, 1, tzinfo=timezone.utc)
    p = _patient(
        consent_sms=False,
        consent_revoked_at=earlier,
        consent_revoked_reason="patient_stop_keyword",
    )
    captured = _patch(monkeypatch, patient=p)

    delta = await h.handle_optout(
        patient_phone="9100", new_user_text="STOP"
    )
    assert delta["audit_reasons"] == ["optout_already"]
    assert "already opted out" in delta["response_body"].lower()
    assert captured["revokes"] == []  # no second revoke
    assert p.consent_revoked_at == earlier  # original timestamp preserved


async def test_handle_optout_renders_in_hindi(monkeypatch):
    p = _patient(consent_sms=True, preferred_language="hi")
    _patch(monkeypatch, patient=p)
    delta = await h.handle_optout(
        patient_phone="9100", new_user_text="STOP"
    )
    # Devanagari "समझ गया" is the leading Hindi ack copy.
    assert "समझ गया" in delta["response_body"]


async def test_handle_optin_restores_consent_and_acks(monkeypatch):
    earlier = datetime(2026, 1, 1, tzinfo=timezone.utc)
    p = _patient(
        consent_sms=False,
        consent_revoked_at=earlier,
        consent_revoked_reason="patient_stop_keyword",
    )
    captured = _patch(monkeypatch, patient=p)

    delta = await h.handle_optin(
        patient_phone="9100", new_user_text="START"
    )
    assert delta["audit_reasons"] == ["optin"]
    assert "welcome back" in delta["response_body"].lower()
    assert captured["restores"] == [p.id]
    assert p.consent_sms is True
    assert p.consent_revoked_at is None


async def test_handle_optin_when_not_opted_out_returns_friendly_ack(monkeypatch):
    """A patient who was never opted out sending START gets a gentle
    "you're already subscribed" reply rather than a misleading
    "welcome back!" one."""
    p = _patient(consent_sms=True, consent_revoked_at=None)
    captured = _patch(monkeypatch, patient=p)

    delta = await h.handle_optin(
        patient_phone="9100", new_user_text="START"
    )
    assert delta["audit_reasons"] == ["optin_not_opted_out"]
    assert "already receiving reminders" in delta["response_body"].lower()
    assert captured["restores"] == []  # no-op


async def test_handle_optout_missing_patient_returns_none(monkeypatch):
    """Defensive — upsert_patient runs upstream so this shouldn't
    happen, but if it does, we return None rather than crashing."""

    def factory():
        return _NoopAsyncSession()

    monkeypatch.setattr(h, "get_sessionmaker", lambda: factory)

    async def get_by_phone(_db, _phone):
        return None

    monkeypatch.setattr(h.patients_repo, "get_by_phone", get_by_phone)

    out = await h.handle_optout(patient_phone="9100", new_user_text="STOP")
    assert out is None

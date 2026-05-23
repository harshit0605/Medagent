"""Unit tests for the doctor-inbox classifier — pure logic, no DB."""

from __future__ import annotations

from services.orchestrator.inbox_classifier import (
    Classification,
    classify_inbound,
    deterministic_for_action_tap,
    is_action_tap,
)


def test_is_action_tap_recognises_known_markers():
    assert is_action_tap("[dose-action] taken adherence_event_id=1")
    assert is_action_tap("[lab-action] booked lab_followup_id=4")
    assert is_action_tap("[refill-action] done regimen_id=2")
    assert is_action_tap("[recap-action] ack recap_id=7")
    assert is_action_tap("[appt-action] cancel appointment_id=99")
    # Whitespace tolerated.
    assert is_action_tap("   [dose-action] taken adherence_event_id=1")


def test_is_action_tap_rejects_freeform():
    assert not is_action_tap("I have chest pain")
    assert not is_action_tap("Can I reschedule?")
    assert not is_action_tap("")
    assert not is_action_tap(None)
    # Looks like a tap but the marker is wrong.
    assert not is_action_tap("[unknown-action] foo bar")


def test_deterministic_action_tap_classification_shape():
    classification = deterministic_for_action_tap()
    assert classification.category == "action_tap"
    assert classification.urgency == "low"
    assert classification.summary  # non-empty deterministic copy


async def test_classify_inbound_action_tap_uses_deterministic_path(monkeypatch):
    """Action-tap inputs must NEVER hit the LLM — they're already
    structured. Asserting via a monkeypatched LLM that would raise if
    called."""
    from services.orchestrator import inbox_classifier

    def explode(*_a, **_k):
        raise AssertionError("LLM should not be called for action-tap inbounds")

    monkeypatch.setattr(inbox_classifier, "get_llm", explode)
    out: Classification = await classify_inbound(
        text="[dose-action] taken adherence_event_id=1"
    )
    assert out.category == "action_tap"


async def test_classify_inbound_empty_text_is_unknown():
    out = await classify_inbound(text=None)
    assert out.category == "unknown"
    assert out.urgency == "low"

    out = await classify_inbound(text="")
    assert out.category == "unknown"


async def test_classify_inbound_falls_back_when_llm_disabled(monkeypatch):
    """When LLM is disabled (e.g. tests, missing API key), the
    classifier returns ``unknown`` with the raw text as the summary —
    so the inbox row is still useful for the doctor without an LLM."""

    class _DisabledLLM:
        enabled = False

        def _get_client(self):
            return None

    from services.orchestrator import inbox_classifier

    monkeypatch.setattr(
        inbox_classifier, "get_llm", lambda: _DisabledLLM()
    )
    out = await classify_inbound(text="My blood sugar reading is 280")
    assert out.category == "unknown"
    # Falls back to the raw inbound as the summary so the inbox isn't
    # blank even without an LLM.
    assert out.summary is not None
    assert "280" in out.summary


def test_system_prompt_pins_summary_language_to_english():
    """The classifier's summary must be in English regardless of
    the patient's typing language. A doctor triaging 50 patients
    across multiple languages needs ONE summary language to scan
    quickly. The patient's verbatim words are preserved separately
    on the row in their original language; the summary is the
    doctor-facing English gloss.

    Without this directive, GPT will sometimes mirror the patient's
    language (Hindi inbound → Hindi summary) which fragments the
    inbox view across languages."""
    from services.orchestrator.inbox_classifier import _SYSTEM_PROMPT

    # The directive must be explicit + uppercase-LANGUAGE so the
    # model treats it as a hard constraint.
    assert "ENGLISH" in _SYSTEM_PROMPT
    assert "regardless of the language" in _SYSTEM_PROMPT.lower()
    # And the rationale must reference the doctor-inbox use case
    # so a future maintainer doesn't accidentally relax it
    # without thinking through the consequence.
    assert "inbox" in _SYSTEM_PROMPT.lower()

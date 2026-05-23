"""Unit tests for the LLM language detector + the gate logic that
decides whether a detection should flip a patient's preferred_language.

The actual LLM call is mocked — we want to assert the GATES (default-
only, length, action-tap exclusion, confidence threshold) without
making real API calls. Each gate has a real bug-prevention story:

- ``default-only``: re-running detection on every inbound would let a
  one-off English sentence flip a Hindi patient back to English.
- ``length``: 1-3 word inputs are unreliable for the model; we'd
  rather leave patients at ``en`` than mis-classify and require
  manual ops correction.
- ``action-tap``: ``[dose-action] taken adherence_event_id=4`` is a
  protocol token. Treating it as English text would still detect ``en``
  but burns a cheap LLM call we don't need.
- ``confidence``: a low-confidence guess on a short message is rarely
  worth flipping someone's preference.
"""

from __future__ import annotations

import types


from services.orchestrator import agent_workflow


def _stub_patient(*, preferred_language: str = "en", id: int = 1):
    return types.SimpleNamespace(
        id=id,
        preferred_language=preferred_language,
        onboarding_step="done",
        full_name="Test",
        phone="91+test",
    )


def _patch_repos(
    monkeypatch,
    *,
    upsert_returns,
    update_lang_calls: list[tuple[int, str]],
):
    """Stub patients_repo + the language-update helper."""

    class _NoopAsyncSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

        async def commit(self):
            return None

    monkeypatch.setattr(
        agent_workflow,
        "_upsert_patient_node",
        agent_workflow._upsert_patient_node,
    )

    async def upsert_by_phone(_db, *, phone):
        return upsert_returns

    async def update_pref_lang(_db, patient_id, *, preferred_language):
        update_lang_calls.append((patient_id, preferred_language))
        upsert_returns.preferred_language = preferred_language
        return upsert_returns

    # Module-level imports inside the node — patch via sys.modules.
    import importlib

    patients_repo = importlib.import_module("app.db.repositories.patients")
    monkeypatch.setattr(patients_repo, "upsert_by_phone", upsert_by_phone)
    monkeypatch.setattr(
        patients_repo,
        "update_preferred_language",
        update_pref_lang,
    )

    session_module = importlib.import_module("app.db.session")
    monkeypatch.setattr(
        session_module, "get_sessionmaker", lambda: _NoopAsyncSession
    )


# ---- Detection gate logic -----------------------------------------------


async def test_action_tap_skips_detection(monkeypatch):
    """``[dose-action] ...`` markers are protocol tokens. The detector
    must NOT be called on them — they'd waste an LLM round-trip and
    a high-confidence ``en`` guess would lock the wrong language for
    a patient whose first real inbound came AFTER the tap."""
    detect_calls: list[str] = []

    class _FakeLLM:
        async def detect_language(self, text: str):
            detect_calls.append(text)
            return ("en", "high")

    monkeypatch.setattr(agent_workflow, "get_llm", lambda: _FakeLLM())
    update_calls: list[tuple[int, str]] = []
    _patch_repos(
        monkeypatch,
        upsert_returns=_stub_patient(),
        update_lang_calls=update_calls,
    )

    state = {
        "phone": "91+test",
        "patient_id": "91+test",
        "text": "[dose-action] taken adherence_event_id=4",
    }
    out = await agent_workflow._upsert_patient_node(state)
    assert out["preferred_language"] == "en"
    assert detect_calls == []  # detector was NEVER called
    assert update_calls == []


async def test_short_text_skips_detection(monkeypatch):
    """Inbound text < 12 chars skips detection — too short to be
    reliable. The patient stays at the default ``en`` until they
    type something longer or ops sets it manually."""
    detect_calls: list[str] = []

    class _FakeLLM:
        async def detect_language(self, text: str):
            detect_calls.append(text)
            return ("hi", "high")

    monkeypatch.setattr(agent_workflow, "get_llm", lambda: _FakeLLM())
    update_calls: list[tuple[int, str]] = []
    _patch_repos(
        monkeypatch,
        upsert_returns=_stub_patient(),
        update_lang_calls=update_calls,
    )

    state = {"phone": "91+test", "patient_id": "91+test", "text": "hi"}
    out = await agent_workflow._upsert_patient_node(state)
    assert detect_calls == []
    assert update_calls == []
    assert out["preferred_language"] == "en"


async def test_high_confidence_hindi_flips_preference(monkeypatch):
    """Long inbound + high-confidence non-English code → flip the
    column. This is the happy path — a fresh Hindi-typing patient
    gets bumped to ``hi`` without operator intervention."""
    class _FakeLLM:
        async def detect_language(self, text: str):
            return ("hi", "high")

    monkeypatch.setattr(agent_workflow, "get_llm", lambda: _FakeLLM())
    update_calls: list[tuple[int, str]] = []
    patient = _stub_patient(preferred_language="en", id=42)
    _patch_repos(
        monkeypatch,
        upsert_returns=patient,
        update_lang_calls=update_calls,
    )

    state = {
        "phone": "91+test",
        "patient_id": "91+test",
        "text": "Mujhe sar mein dard ho raha hai dawai bata do",
    }
    out = await agent_workflow._upsert_patient_node(state)
    assert update_calls == [(42, "hi")]
    assert out["preferred_language"] == "hi"


async def test_low_confidence_does_not_flip(monkeypatch):
    """Low / medium confidence detections leave the patient at ``en``.
    The directive is conservative — false flips are worse than missed
    flips because ops correction is one click."""
    class _FakeLLM:
        async def detect_language(self, text: str):
            return ("hi", "low")

    monkeypatch.setattr(agent_workflow, "get_llm", lambda: _FakeLLM())
    update_calls: list[tuple[int, str]] = []
    _patch_repos(
        monkeypatch,
        upsert_returns=_stub_patient(),
        update_lang_calls=update_calls,
    )

    state = {
        "phone": "91+test",
        "patient_id": "91+test",
        "text": "kya hai bhai chal raha hai sab kuch",
    }
    out = await agent_workflow._upsert_patient_node(state)
    assert update_calls == []
    assert out["preferred_language"] == "en"


async def test_already_non_default_skips_detection(monkeypatch):
    """Once preferred_language is set to anything other than ``en``,
    detection no longer runs — the value is sticky so a one-off
    English message from a Hindi-preferring patient doesn't flicker
    their preference back to ``en``."""
    detect_calls: list[str] = []

    class _FakeLLM:
        async def detect_language(self, text: str):
            detect_calls.append(text)
            return ("en", "high")

    monkeypatch.setattr(agent_workflow, "get_llm", lambda: _FakeLLM())
    update_calls: list[tuple[int, str]] = []
    _patch_repos(
        monkeypatch,
        upsert_returns=_stub_patient(preferred_language="hi"),
        update_lang_calls=update_calls,
    )

    state = {
        "phone": "91+test",
        "patient_id": "91+test",
        "text": "Hello can you tell me about my prescription",
    }
    out = await agent_workflow._upsert_patient_node(state)
    assert detect_calls == []
    assert update_calls == []
    assert out["preferred_language"] == "hi"


async def test_unknown_code_does_not_flip(monkeypatch):
    """The detector returning ``unknown`` (its catch-all) must not flip
    anything. The literal ``unknown`` is in the schema so the model
    can express "I'm not sure" instead of forcing a wrong guess."""
    class _FakeLLM:
        async def detect_language(self, text: str):
            return ("unknown", "high")

    monkeypatch.setattr(agent_workflow, "get_llm", lambda: _FakeLLM())
    update_calls: list[tuple[int, str]] = []
    _patch_repos(
        monkeypatch,
        upsert_returns=_stub_patient(),
        update_lang_calls=update_calls,
    )

    state = {
        "phone": "91+test",
        "patient_id": "91+test",
        "text": "qwerty asdfghjkl mixed nonsense maybe",
    }
    out = await agent_workflow._upsert_patient_node(state)
    assert update_calls == []
    assert out["preferred_language"] == "en"


async def test_detector_failure_falls_through(monkeypatch):
    """LLM detector returning None (network / disabled) must not
    crash the upsert path. The patient gets the default ``en`` and
    the workflow proceeds normally."""
    class _FakeLLM:
        async def detect_language(self, text: str):
            return None

    monkeypatch.setattr(agent_workflow, "get_llm", lambda: _FakeLLM())
    update_calls: list[tuple[int, str]] = []
    _patch_repos(
        monkeypatch,
        upsert_returns=_stub_patient(),
        update_lang_calls=update_calls,
    )

    state = {
        "phone": "91+test",
        "patient_id": "91+test",
        "text": "I have been having trouble sleeping",
    }
    out = await agent_workflow._upsert_patient_node(state)
    assert update_calls == []
    assert out["preferred_language"] == "en"

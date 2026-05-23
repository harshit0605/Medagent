"""Integration tests for the doctor reply drafter (slice 13).

Covered:
    1. Drafter degrades gracefully when LLM is disabled —
       returns empty draft + low confidence + a "type
       manually" caveat. UI relies on this.
    2. Drafter forwards stubbed LLM output verbatim, modulo
       length + caveat truncation.
    3. Open critical alert auto-injects a caveat AND forces
       confidence to ``low``, even if the LLM self-rated
       higher. Belt-and-suspenders against a model that
       might miss the alert in its own context window.
    4. Empty inbound text returns an empty draft + caveat
       without burning LLM tokens.
    5. /ops/inbox/{id}/draft-reply endpoint round-trip.
    6. 404 for missing classification, 400 for action-tap
       rows (no inbound_text).

DATABASE_URL required.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.db.models import (
    InboundClassification,
    Patient,
)
from app.db.repositories import (
    clinical_alerts as clinical_alerts_repo,
    inbound_classifications as inbound_classifications_repo,
)
from app.db.session import get_sessionmaker
from services.orchestrator import doctor_reply_drafter

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping drafter tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


# ---- LLM stub -------------------------------------------------------------


class _FakeMessage:
    def __init__(self, parsed: Any) -> None:
        self.parsed = parsed


class _FakeChoice:
    def __init__(self, parsed: Any) -> None:
        self.message = _FakeMessage(parsed)


class _FakeUsage:
    def __init__(self) -> None:
        self.prompt_tokens = 100
        self.completion_tokens = 60
        self.total_tokens = 160


class _FakeCompletion:
    def __init__(self, parsed: Any) -> None:
        self.choices = [_FakeChoice(parsed)]
        self.usage = _FakeUsage()


class _FakeAsyncCompletions:
    def __init__(
        self, parsed: Any, *, raise_exc: Exception | None = None
    ) -> None:
        self._parsed = parsed
        self._raise = raise_exc

    async def parse(self, *, model, response_format, messages):
        if self._raise is not None:
            raise self._raise
        parsed_obj = response_format(**self._parsed)
        return _FakeCompletion(parsed_obj)


class _FakeChat:
    def __init__(self, completions: _FakeAsyncCompletions) -> None:
        self.completions = completions


class _FakeClient:
    def __init__(self, completions: _FakeAsyncCompletions) -> None:
        self.chat = _FakeChat(completions)


class _FakeLLM:
    def __init__(
        self, parsed: Any, *, raise_exc: Exception | None = None
    ) -> None:
        self.enabled = True
        self.model = "gpt-4o-mini"
        self._client = _FakeClient(
            _FakeAsyncCompletions(parsed, raise_exc=raise_exc)
        )

    def _get_client(self):
        return self._client


# ---- helpers --------------------------------------------------------------


async def _seed_patient() -> tuple[int, str]:
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(
            full_name=f"Drafter Test {suffix}",
            phone=f"drft-{suffix}",
            consent_sms=True,
        )
        db.add(p)
        await db.flush()
        await db.commit()
        return p.id, p.phone


async def _seed_classification(
    *,
    patient_id: int,
    phone: str,
    inbound_text: str = "thanks doc",
) -> int:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        row = await inbound_classifications_repo.create(
            db,
            message_id=f"wamid-{uuid.uuid4().hex[:8]}",
            patient_phone=phone,
            patient_db_id=patient_id,
            inbound_text=inbound_text,
            category="social",
            summary="patient says thanks",
            urgency="low",
            handler_used="llm_compose",
            response_text="—",
            escalated=False,
            ticket_id=None,
            input_kind="text",
        )
        await db.commit()
        return row.id


async def _seed_critical_alert(
    *, patient_id: int, phone: str
) -> int:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        row = await clinical_alerts_repo.create(
            db,
            patient_id=patient_id,
            patient_phone=phone,
            message_id=None,
            severity="critical",
            red_flags=["chest pain"],
            clinical_summary="prior chest-pain alert still open",
            inbound_text="x",
            llm_model="test",
        )
        await db.commit()
        return row.id


# ---- drafter service tests ------------------------------------------------


async def test_drafter_returns_disabled_placeholder_by_default():
    """Default test env has LLM_ENABLED=0 — drafter must
    return empty draft + low confidence + caveat instead of
    raising. UI relies on this graceful fallback."""
    pid, _ = await _seed_patient()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await doctor_reply_drafter.draft_reply(
            db,
            patient_id=pid,
            inbound_text="hi doc, when's my next appt?",
        )

    assert result.draft_text == ""
    assert result.confidence == "low"
    assert any("LLM disabled" in c for c in result.caveats)


async def test_drafter_empty_text_returns_caveat_without_llm():
    pid, _ = await _seed_patient()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await doctor_reply_drafter.draft_reply(
            db, patient_id=pid, inbound_text="   "
        )

    assert result.draft_text == ""
    assert result.confidence == "low"
    assert any(
        "empty inbound" in c.lower() for c in result.caveats
    )


async def test_drafter_forwards_stubbed_llm_output(monkeypatch):
    pid, _ = await _seed_patient()

    fake = _FakeLLM(
        parsed={
            "draft_text": "Hi Anna, your next visit is Friday at 3pm.",
            "confidence": "high",
            "caveats": [],
        }
    )
    monkeypatch.setattr(
        doctor_reply_drafter, "get_llm", lambda: fake
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await doctor_reply_drafter.draft_reply(
            db,
            patient_id=pid,
            inbound_text="when is my next appointment?",
            patient_first_name="Anna",
        )

    assert "Friday" in result.draft_text
    assert result.confidence == "high"
    assert result.caveats == []
    assert result.prompt_tokens == 100
    assert result.completion_tokens == 60


async def test_drafter_truncates_oversized_draft(monkeypatch):
    pid, _ = await _seed_patient()
    long_text = "A" * (
        doctor_reply_drafter.MAX_DRAFT_CHARS + 200
    )

    fake = _FakeLLM(
        parsed={
            "draft_text": long_text,
            "confidence": "medium",
            "caveats": [],
        }
    )
    monkeypatch.setattr(
        doctor_reply_drafter, "get_llm", lambda: fake
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await doctor_reply_drafter.draft_reply(
            db, patient_id=pid, inbound_text="test"
        )

    assert (
        len(result.draft_text)
        == doctor_reply_drafter.MAX_DRAFT_CHARS
    )


async def test_drafter_truncates_caveat_list(monkeypatch):
    pid, _ = await _seed_patient()

    fake = _FakeLLM(
        parsed={
            "draft_text": "ok.",
            "confidence": "medium",
            "caveats": [f"caveat {i}" for i in range(20)],
        }
    )
    monkeypatch.setattr(
        doctor_reply_drafter, "get_llm", lambda: fake
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await doctor_reply_drafter.draft_reply(
            db, patient_id=pid, inbound_text="test"
        )

    assert len(result.caveats) == doctor_reply_drafter.MAX_CAVEATS


async def test_drafter_open_critical_alert_forces_low_confidence(
    monkeypatch,
):
    """Even if the LLM self-rates ``high``, an open critical
    alert on this patient forces confidence to ``low`` and
    appends an explicit caveat. The reviewer must consciously
    confirm before sending."""
    pid, phone = await _seed_patient()
    await _seed_critical_alert(patient_id=pid, phone=phone)

    fake = _FakeLLM(
        parsed={
            "draft_text": "Sure, let me know if anything else.",
            "confidence": "high",
            "caveats": [],
        }
    )
    monkeypatch.setattr(
        doctor_reply_drafter, "get_llm", lambda: fake
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await doctor_reply_drafter.draft_reply(
            db, patient_id=pid, inbound_text="ok thanks"
        )

    assert result.confidence == "low"
    assert any(
        "critical" in c.lower() and "alert" in c.lower()
        for c in result.caveats
    )


async def test_drafter_does_not_double_inject_critical_caveat(
    monkeypatch,
):
    """If the LLM already mentioned the critical alert in its
    caveats, we don't add a duplicate."""
    pid, phone = await _seed_patient()
    await _seed_critical_alert(patient_id=pid, phone=phone)

    fake = _FakeLLM(
        parsed={
            "draft_text": "Sure.",
            "confidence": "low",
            "caveats": [
                "Patient has an open critical alert from yesterday."
            ],
        }
    )
    monkeypatch.setattr(
        doctor_reply_drafter, "get_llm", lambda: fake
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await doctor_reply_drafter.draft_reply(
            db, patient_id=pid, inbound_text="ok thanks"
        )

    matches = [
        c
        for c in result.caveats
        if "critical" in c.lower() and "alert" in c.lower()
    ]
    assert len(matches) == 1


async def test_drafter_llm_failure_returns_caveat(monkeypatch):
    pid, _ = await _seed_patient()

    fake = _FakeLLM(
        parsed={
            "draft_text": "",
            "confidence": "low",
            "caveats": [],
        },
        raise_exc=ConnectionError("simulated network down"),
    )
    monkeypatch.setattr(
        doctor_reply_drafter, "get_llm", lambda: fake
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await doctor_reply_drafter.draft_reply(
            db, patient_id=pid, inbound_text="hi"
        )

    assert result.draft_text == ""
    assert result.confidence == "low"
    assert any(
        "AI draft failed" in c for c in result.caveats
    )


# ---- endpoint tests -------------------------------------------------------


async def test_endpoint_round_trip(orchestrator_client):
    pid, phone = await _seed_patient()
    cid = await _seed_classification(
        patient_id=pid, phone=phone, inbound_text="hi doc"
    )

    response = orchestrator_client.post(
        f"/ops/inbox/{cid}/draft-reply"
    )
    assert response.status_code == 200
    body = response.json()
    # LLM disabled in test env → empty draft + low confidence
    # + caveat. Endpoint shape is what we care about here.
    assert "draft_text" in body
    assert body["confidence"] == "low"
    assert isinstance(body["caveats"], list)


async def test_endpoint_404_for_missing_classification(
    orchestrator_client,
):
    response = orchestrator_client.post(
        "/ops/inbox/999999999/draft-reply"
    )
    assert response.status_code == 404


async def test_endpoint_400_for_no_inbound_text(orchestrator_client):
    """Action-tap and image-only rows have no body to draft
    against — endpoint returns 400 rather than burning tokens
    on placeholder input."""
    pid, phone = await _seed_patient()
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        row = await inbound_classifications_repo.create(
            db,
            message_id=f"wamid-{uuid.uuid4().hex[:8]}",
            patient_phone=phone,
            patient_db_id=pid,
            inbound_text=None,
            category="action_tap",
            summary=None,
            urgency="low",
            handler_used=None,
            response_text=None,
            escalated=False,
            ticket_id=None,
            input_kind="button",
        )
        await db.commit()
        cid = row.id

    response = orchestrator_client.post(
        f"/ops/inbox/{cid}/draft-reply"
    )
    assert response.status_code == 400


async def test_endpoint_400_for_missing_patient_db_id(
    orchestrator_client,
):
    """An inbox row with no resolved patient_db_id (e.g.
    inbound from an unknown phone) can't be personalised. We
    fail fast rather than draft something generic."""
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        row = InboundClassification(
            message_id=f"wamid-{suffix}",
            patient_phone=f"unknown-{suffix}",
            patient_db_id=None,
            inbound_text="random message from unknown patient",
            input_kind="text",
            category="unknown",
            summary=None,
            urgency="low",
            handler_used=None,
            response_text=None,
            escalated=False,
            ticket_id=None,
        )
        db.add(row)
        await db.flush()
        await db.commit()
        cid = row.id

    response = orchestrator_client.post(
        f"/ops/inbox/{cid}/draft-reply"
    )
    assert response.status_code == 400


async def test_endpoint_with_stubbed_llm(
    monkeypatch, orchestrator_client
):
    """Stub the LLM so the endpoint returns a non-empty
    draft. Confirms the patient context (regimens, alerts,
    timeline) is gathered without erroring out."""
    pid, phone = await _seed_patient()
    cid = await _seed_classification(
        patient_id=pid,
        phone=phone,
        inbound_text="when's my next visit?",
    )

    fake = _FakeLLM(
        parsed={
            "draft_text": "Hi! Your next visit is on Friday.",
            "confidence": "high",
            "caveats": [],
        }
    )
    monkeypatch.setattr(
        doctor_reply_drafter, "get_llm", lambda: fake
    )

    response = orchestrator_client.post(
        f"/ops/inbox/{cid}/draft-reply"
    )
    assert response.status_code == 200
    body = response.json()
    assert "Friday" in body["draft_text"]
    assert body["confidence"] == "high"

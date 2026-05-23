"""Integration tests for the /route voice-note path (DB-backed).

Proves a spoken message is transcribed at the /route entry and the
*transcript* — not the raw ``[voice-note]`` marker — flows through the rest
of the pipeline:

  * a voice note saying "sugar 155" lands as a ``metric_observation`` (via
    the deterministic vitals short-circuit), proving the transcript reached
    routing/handling, and the inbound is badged ``input_kind=voice``;
  * when transcription is unavailable, /route still 200s (degrades to the
    typed-fallback copy) and writes no spurious reading.

faster-whisper is never invoked — we monkeypatch ``maybe_transcribe`` so the
test is deterministic and needs no audio file or model weights.

Skipped when DATABASE_URL is unset.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.db.models import Patient
from app.db.repositories import care_plan_goals as goals_repo
from app.db.repositories import inbound_classifications as ic_repo
from app.db.session import get_sessionmaker
from services.orchestrator import transcription

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping voice route integration tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


async def _seed_patient() -> tuple[int, str]:
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(
            full_name=f"Voice Test {suffix}",
            phone=f"voice-{suffix}",
            consent_sms=True,
            cohort_diabetes=True,
        )
        db.add(p)
        await db.flush()
        await db.commit()
        return p.id, p.phone


async def _observations(patient_id: int, metric_key: str) -> list:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        return await goals_repo.list_observations_for_patient(
            db, patient_id, metric_key=metric_key
        )


_MARKER = "[voice-note] public_path=/uploads/voice/{id}.ogg mime=audio/ogg"


async def test_voice_note_transcribes_to_vitals_and_badges_voice(
    orchestrator_client, monkeypatch
):
    # The spoken words decode to a glucose reading. We patch the transcriber
    # (not the marker detector) so the real /route detection + text-mutation
    # wiring is exercised end-to-end.
    monkeypatch.setattr(transcription, "maybe_transcribe", lambda _text: "sugar 155")

    pid, phone = await _seed_patient()
    resp = orchestrator_client.post(
        "/route",
        json={
            "message": {
                "patient_id": phone,
                "text": _MARKER.format(id=uuid.uuid4().hex[:6]),
                "message_id": f"wamid-{uuid.uuid4().hex[:8]}",
            },
        },
    )
    assert resp.status_code == 200

    # The transcript reached the vitals short-circuit → observation written.
    rows = await _observations(pid, "blood_glucose")
    assert len(rows) == 1
    assert rows[0].value == Decimal("155.000")
    assert rows[0].source == "patient_self_report"

    # The inbound is badged as voice so the ops inbox shows a voice chip.
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        classifications = await ic_repo.list_recent(db, patient_phone=phone, limit=10)
    assert classifications, "no inbound_classification persisted"
    assert classifications[0].input_kind == "voice"


async def test_voice_note_fallback_when_transcription_unavailable(
    orchestrator_client, monkeypatch
):
    """When whisper is unavailable, ``maybe_transcribe`` yields the typed-
    fallback copy. /route must still 200 (degrade politely) and write no
    vitals observation — the fallback text isn't a reading."""
    monkeypatch.setattr(
        transcription,
        "maybe_transcribe",
        lambda _text: transcription._FALLBACK_TEXT,
    )

    pid, phone = await _seed_patient()
    resp = orchestrator_client.post(
        "/route",
        json={
            "message": {
                "patient_id": phone,
                "text": _MARKER.format(id=uuid.uuid4().hex[:6]),
                "message_id": f"wamid-{uuid.uuid4().hex[:8]}",
            },
        },
    )
    assert resp.status_code == 200
    rows = await _observations(pid, "blood_glucose")
    assert rows == []

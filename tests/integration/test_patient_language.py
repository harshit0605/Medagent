"""Integration tests for the patient preferred_language endpoint +
the recap generator's language plumbing.

Covers:
- /i18n/languages endpoint shape.
- PUT /patients/{id}/preferred-language: 400 on unsupported, 404 on
  unknown patient, 200 + persisted column on happy path.
- /patients/{id} detail returns the persisted preferred_language.
- _build_recap_context sets preferred_language from the patient row
  (smoke-tested via the preview endpoint, which builds the context).
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.db.models import (
    Appointment,
    AppointmentStatus,
    Doctor,
    DoctorOAuthStatus,
    Patient,
)
from app.db.session import get_sessionmaker

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping language integration tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


def test_supported_languages_endpoint(orchestrator_client):
    """Endpoint returns the static allowlist with code + label.
    Mirrors app.i18n.SUPPORTED_LANGUAGES so adding a language is a
    constants-only change."""
    r = orchestrator_client.get("/i18n/languages")
    assert r.status_code == 200
    body = r.json()
    codes = {opt["code"] for opt in body}
    assert {"en", "hi", "ta"} <= codes
    # Each entry has a non-empty label.
    for opt in body:
        assert opt["label"]


async def test_update_language_happy_path(orchestrator_client):
    """PUT preferred_language with a valid code persists the column +
    is reflected on the patient detail."""
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(
            full_name=f"Lang Test {suffix}",
            phone=f"lang-test-{suffix}",
        )
        db.add(p)
        await db.flush()
        await db.commit()
        patient_id = p.id

    r = orchestrator_client.put(
        f"/patients/{patient_id}/preferred-language",
        json={"preferred_language": "hi"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["preferred_language"] == "hi"

    # Verify the column persisted by reading via the detail endpoint.
    detail = orchestrator_client.get(f"/patients/{patient_id}").json()
    assert detail["preferred_language"] == "hi"


def test_update_language_400_for_unsupported_code(orchestrator_client):
    """The endpoint validates against app.i18n.SUPPORTED_LANGUAGE_CODES.
    A garbage code returns 400 with a helpful detail listing the
    allowed codes."""
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()

    async def _seed() -> int:
        async with SessionLocal() as db:
            p = Patient(
                full_name=f"Lang Bad {suffix}",
                phone=f"lang-bad-{suffix}",
            )
            db.add(p)
            await db.flush()
            await db.commit()
            return p.id

    import asyncio

    patient_id = asyncio.get_event_loop().run_until_complete(_seed())
    r = orchestrator_client.put(
        f"/patients/{patient_id}/preferred-language",
        json={"preferred_language": "xx"},
    )
    assert r.status_code == 400
    assert "unsupported" in r.json()["detail"].lower()


def test_update_language_404_for_unknown_patient(orchestrator_client):
    r = orchestrator_client.put(
        "/patients/9999999/preferred-language",
        json={"preferred_language": "hi"},
    )
    assert r.status_code == 404


async def test_recap_context_carries_preferred_language(orchestrator_client):
    """_build_recap_context inlines patient.preferred_language into the
    RecapContext so generate_recap can hint the LLM. End-to-end check:
    set the patient's language to Hindi, draft + preview a recap, and
    verify the orchestrator returns OK (the LLM's actual output is a
    deterministic-fallback when LLM is disabled in tests, which is
    English-only — but the codepath ran without crashing on the new
    field, which is what matters here)."""
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        patient = Patient(
            full_name=f"Recap Lang Test {suffix}",
            phone=f"recap-lang-{suffix}",
            preferred_language="hi",
        )
        doctor = Doctor(
            name=f"Dr Recap Lang {suffix}",
            email=f"dr-rl-{suffix}@example.com",
            timezone="UTC",
            calendar_id="primary",
            oauth_status=DoctorOAuthStatus.connected,
        )
        db.add_all([patient, doctor])
        await db.flush()
        when = datetime.now(timezone.utc) - timedelta(hours=2)
        appt = Appointment(
            patient_id=patient.id,
            doctor_id=doctor.id,
            scheduled_for=when,
            end_at=when + timedelta(minutes=30),
            status=AppointmentStatus.completed,
            source="test",
        )
        db.add(appt)
        await db.flush()
        await db.commit()
        appt_id = appt.id

    # Draft + preview — exercises _build_recap_context which now reads
    # patient.preferred_language. Must not error.
    orchestrator_client.put(
        f"/appointments/{appt_id}/recap",
        json={
            "doctor_notes": "Hindi-language recap test",
            "structured": {},
        },
    )
    r = orchestrator_client.post(
        f"/appointments/{appt_id}/recap/preview"
    )
    assert r.status_code == 200
    # The body comes back populated either way (deterministic fallback
    # when LLM is off in tests, or LLM-generated otherwise).
    assert r.json()["body"]


def test_default_language_is_en_for_new_patient(orchestrator_client):
    """Newly-created patients default to ``en`` via the column's
    server_default. Catch a regression where a future migration drops
    or overrides the default."""
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()

    async def _seed() -> int:
        async with SessionLocal() as db:
            p = Patient(
                full_name=f"Default Lang {suffix}",
                phone=f"default-lang-{suffix}",
            )
            db.add(p)
            await db.flush()
            await db.commit()
            return p.id

    import asyncio

    patient_id = asyncio.get_event_loop().run_until_complete(_seed())
    detail = orchestrator_client.get(f"/patients/{patient_id}").json()
    assert detail["preferred_language"] == "en"

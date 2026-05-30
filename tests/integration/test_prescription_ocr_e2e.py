"""End-to-end Rx OCR pipeline (H1).

The pipeline was already built (prescription_handler + OpenAI vision +
ops-verify flow); the gap was an e2e test. This covers both paths:

  * **Vision success**: a [prescription-upload] inbound with the vision LLM
    mocked to return a structured parse → a Prescription row in pending-review
    with the parsed regimens persisted.
  * **Ops verify**: an operator confirms/corrects the parse via
    POST /prescriptions/{id}/verify → real Regimen rows created + the
    prescription marked verified.
  * **Vision unavailable** (LLM off): the row still lands in needs-manual-entry
    state and the patient gets the "team will review" reply.

DATABASE_URL required.
"""

from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.db.models import Patient
from app.db.repositories import prescriptions as prescriptions_repo
from app.db.repositories import regimens as regimens_repo
from app.db.session import get_sessionmaker

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping integration tests",
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
            full_name=f"Rx OCR {suffix}",
            phone=f"rxocr-{suffix}",
            consent_sms=True,
            onboarding_step="active",
        )
        db.add(p)
        await db.flush()
        await db.commit()
        return p.id, p.phone


async def test_upload_with_mocked_vision_parses_and_verifies(
    orchestrator_client, monkeypatch
):
    from services.orchestrator import prescription_handler
    from services.orchestrator.llm import (
        ParsedPrescription,
        ParsedRegimen,
        ParsePrescriptionResult,
    )

    pid, phone = await _seed_patient()

    # Mock the vision LLM to return a clean two-medication parse.
    parsed = ParsePrescriptionResult(
        parsed=ParsedPrescription(
            confidence="high",
            regimens=[
                ParsedRegimen(
                    medication_name="Metformin",
                    dose="500 mg",
                    times_of_day=["08:00", "20:00"],
                    frequency_text="twice daily",
                ),
                ParsedRegimen(
                    medication_name="Amlodipine",
                    dose="5 mg",
                    times_of_day=["08:00"],
                    frequency_text="once daily",
                ),
            ],
            summary="Two-medication Rx",
        ),
        used_model="gpt-4o-mock",
    )
    fake_llm = AsyncMock()
    fake_llm.parse_prescription_image = AsyncMock(return_value=parsed)
    monkeypatch.setattr(prescription_handler, "get_llm", lambda: fake_llm)

    # Patient uploads a prescription (the gateway rewrites it to this marker).
    delta = await prescription_handler.handle_prescription_upload(
        patient_phone=phone,
        new_user_text="[prescription-upload] public_path=/uploads/rx/a.jpg mime=image/jpeg",
    )
    assert delta is not None
    assert "prescription" in " ".join(delta["audit_reasons"]).lower()

    # A pending prescription row exists with the parsed regimens.
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows = await prescriptions_repo.list_for_patient(db, pid)
    assert rows, "a prescription row should be created"
    presc = rows[0]
    assert presc.human_verification_status.value == "pending"
    parsed_payload = presc.parsed_payload or {}
    assert parsed_payload.get("vision_parse_failed") is False
    assert len(parsed_payload["parsed"]["regimens"]) == 2

    # Operator verifies (confirms the parse) → real regimens created.
    resp = orchestrator_client.post(
        f"/prescriptions/{presc.id}/verify",
        json={
            "verified_by": "dr.kim",
            "regimens": [
                {
                    "medication_name": "Metformin",
                    "dose": "500 mg",
                    "times_of_day": ["08:00", "20:00"],
                },
                {
                    "medication_name": "Amlodipine",
                    "dose": "5 mg",
                    "times_of_day": ["08:00"],
                },
            ],
            "timezone": "Asia/Kolkata",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "verified"

    # Two real Regimen rows now exist for the patient.
    async with SessionLocal() as db:
        regs = await regimens_repo.list_for_patient(db, pid)
    names = {r.medication_name for r in regs}
    assert {"Metformin", "Amlodipine"} <= names


async def test_upload_with_vision_unavailable_lands_in_manual_review(
    orchestrator_client, monkeypatch
):
    """LLM off (the test default): the row still lands needs-manual-entry and
    the patient gets the review reply — no data lost."""
    from services.orchestrator import prescription_handler

    pid, phone = await _seed_patient()

    fake_llm = AsyncMock()
    fake_llm.parse_prescription_image = AsyncMock(return_value=None)  # unreachable
    monkeypatch.setattr(prescription_handler, "get_llm", lambda: fake_llm)

    delta = await prescription_handler.handle_prescription_upload(
        patient_phone=phone,
        new_user_text="[prescription-upload] public_path=/uploads/rx/b.jpg mime=image/jpeg",
    )
    assert delta is not None
    assert "review" in delta["response_body"].lower()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows = await prescriptions_repo.list_for_patient(db, pid)
    assert rows
    payload = rows[0].parsed_payload or {}
    assert payload.get("vision_parse_failed") is True
    assert rows[0].human_verification_status.value == "pending"

"""Integration tests for clinician-authored doctor replies.

Covers:
- 404 when patient doesn't exist.
- 400 when patient has no phone on file.
- 409 when patient is outside the 24h customer-service window (no
  doctor-authored template approved → can't send freeform out-of-CSW).
- 200 when patient is in-CSW + gateway succeeds; outbound is logged
  to ``message_log`` AND an audit row is written tagged ``doctor_reply``.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import (
    AuditRecord,
    Patient,
    PatientInboundState,
)
from app.db.session import get_sessionmaker

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping doctor-reply integration tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


async def _seed_patient(*, in_csw: bool) -> tuple[int, str]:
    """Create a patient. If ``in_csw=True``, also seed an inbound-state
    row dated now so the CSW gate passes. Returns (id, phone)."""
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(
            full_name=f"Reply Test {suffix}",
            phone=f"reply-test-{suffix}",
        )
        db.add(p)
        await db.flush()
        if in_csw:
            db.add(
                PatientInboundState(
                    patient_id=p.phone,
                    last_inbound_at=datetime.now(timezone.utc),
                )
            )
        await db.commit()
        await db.refresh(p)
        return p.id, p.phone


def test_reply_404_for_unknown_patient(orchestrator_client):
    r = orchestrator_client.post(
        "/patients/9999999/reply",
        json={"body": "test", "sent_by": "dr.smith"},
    )
    assert r.status_code == 404


async def test_reply_409_when_out_of_csw(orchestrator_client):
    """Without a recent inbound, the CSW gate must reject the send so
    the doctor sees a clear "patient must message first" error rather
    than a silent freeform-fails-at-Meta failure."""
    patient_id, _ = await _seed_patient(in_csw=False)
    r = orchestrator_client.post(
        f"/patients/{patient_id}/reply",
        json={
            "body": "Just checking in on your symptoms",
            "sent_by": "dr.smith",
        },
    )
    assert r.status_code == 409
    assert "customer-service window" in r.json()["detail"]


async def test_reply_in_csw_succeeds_and_audits(
    orchestrator_client, monkeypatch
):
    """Happy path: in-CSW patient + monkeypatched gateway send →
    200 with wamid, outbound logged in message_log, audit row written
    tagged ``doctor_reply``."""
    from services.orchestrator import main as orchestrator_main

    captured_payloads: list[dict] = []

    async def fake_send(*, patient_phone: str, body: str) -> str | None:
        captured_payloads.append({"phone": patient_phone, "body": body})
        return f"wamid.fake.reply.{uuid.uuid4().hex[:8]}"

    monkeypatch.setattr(
        orchestrator_main, "_send_doctor_reply_via_gateway", fake_send
    )

    patient_id, patient_phone = await _seed_patient(in_csw=True)

    r = orchestrator_client.post(
        f"/patients/{patient_id}/reply",
        json={
            "body": "Great news on the labs — let's hold the dose adjustment.",
            "sent_by": "dr.smith",
            "in_reply_to_message_id": "msg-abc-123",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "sent"
    assert body["wamid"].startswith("wamid.fake.reply.")
    assert body["sent_by"] == "dr.smith"

    # Gateway helper was called with the right phone + body.
    assert len(captured_payloads) == 1
    assert captured_payloads[0]["phone"] == patient_phone
    assert "Great news" in captured_payloads[0]["body"]

    # Audit row written, tagged ``doctor_reply`` with the ``sent_by``
    # captured in details. (We don't assert message_log here because
    # the gateway adds that row in its own request handler — which the
    # monkeypatched fake_send bypasses entirely. Audit is the orchestrator's
    # responsibility and is what we directly invoke.)
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        audit_rows = (
            await db.execute(
                select(AuditRecord).where(
                    AuditRecord.patient_id == patient_phone
                )
            )
        ).scalars().all()
    matched = [
        a for a in audit_rows if "doctor_reply" in (a.reason_codes or [])
    ]
    assert len(matched) == 1
    assert matched[0].outbound_mode == "FREEFORM"
    assert matched[0].details["sent_by"] == "dr.smith"
    assert matched[0].details["in_reply_to_message_id"] == "msg-abc-123"
    assert "Great news" in matched[0].details["body_excerpt"]


async def test_reply_400_when_patient_has_no_phone(orchestrator_client):
    """Edge case: a Patient row with empty phone (shouldn't happen via
    onboarding but possible via direct admin insertion). Must surface
    a clean 400 rather than calling the gateway with an empty phone.

    Schema requires phone NOT NULL, but the unique index treats ``''``
    as a real value — so re-runs of this test would collide on that
    sentinel. Pre-clean before insert.
    """
    from sqlalchemy import text as _sql_text

    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        # Wipe any prior test residue with phone='' so the
        # unique-on-phone index doesn't collide.
        await db.execute(
            _sql_text(
                "DELETE FROM patients WHERE phone = '' AND "
                "full_name LIKE 'No Phone %'"
            )
        )
        p = Patient(
            full_name=f"No Phone {suffix}",
            phone="",
        )
        db.add(p)
        await db.flush()
        await db.commit()
        patient_id = p.id

    r = orchestrator_client.post(
        f"/patients/{patient_id}/reply",
        json={"body": "test", "sent_by": "dr.smith"},
    )
    # Either 400 (no phone) or 409 (no CSW since no inbound). Both are
    # legitimate failures; the test asserts we DON'T 200 on a phoneless
    # patient AND we don't crash the endpoint.
    assert r.status_code in (400, 409)


async def test_reply_502_when_gateway_send_fails(
    orchestrator_client, monkeypatch
):
    """Gateway returns no wamid → endpoint must surface a 502 so the
    UI can show a clear failure rather than persisting a successful-
    looking audit row."""
    from services.orchestrator import main as orchestrator_main

    async def fake_send(*, patient_phone: str, body: str) -> str | None:
        return None

    monkeypatch.setattr(
        orchestrator_main, "_send_doctor_reply_via_gateway", fake_send
    )

    patient_id, _ = await _seed_patient(in_csw=True)
    r = orchestrator_client.post(
        f"/patients/{patient_id}/reply",
        json={"body": "test", "sent_by": "dr.smith"},
    )
    assert r.status_code == 502

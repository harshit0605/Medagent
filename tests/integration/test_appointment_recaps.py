"""Integration tests for the appointment recap endpoints.

Skipped when DATABASE_URL is unset. Each test creates a fresh patient +
doctor + appointment so reruns don't collide.
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
    reason="DATABASE_URL not set — skipping recap integration tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


async def _seed_appointment() -> int:
    """Create a fresh patient + doctor + appointment, return appointment id."""
    SessionLocal = get_sessionmaker()
    suffix = uuid.uuid4().hex[:8]
    async with SessionLocal() as db:
        patient = Patient(
            full_name=f"Recap Test {suffix}",
            phone=f"recap-test-{suffix}",
        )
        db.add(patient)
        await db.flush()
        doctor = Doctor(
            name=f"Dr. Recap {suffix}",
            email=f"dr-recap-{suffix}@example.com",
            timezone="UTC",
            calendar_id="primary",
            oauth_status=DoctorOAuthStatus.connected,
        )
        db.add(doctor)
        await db.flush()
        when = datetime.now(timezone.utc) - timedelta(hours=2)
        appointment = Appointment(
            patient_id=patient.id,
            doctor_id=doctor.id,
            scheduled_for=when,
            end_at=when + timedelta(minutes=30),
            status=AppointmentStatus.completed,
            source="test",
            summary="HbA1c review",
        )
        db.add(appointment)
        await db.flush()
        await db.commit()
        return appointment.id


async def test_recap_lifecycle_draft_preview_then_404_send(orchestrator_client):
    appointment_id = await _seed_appointment()

    # GET on absent recap returns null (not 404).
    response = orchestrator_client.get(
        f"/appointments/{appointment_id}/recap"
    )
    assert response.status_code == 200
    assert response.json() in (None, {"status_code": 200})

    # PUT creates the draft.
    payload = {
        "doctor_notes": "Continue plan",
        "structured": {
            "meds_added": [
                {"name": "Vitamin D3", "instructions": "1 tab daily"}
            ],
            "meds_changed": [],
            "meds_stopped": [],
            "labs_ordered": [{"test_name": "HbA1c"}],
            "next_followup_in_days": 60,
            "red_flags": ["chest pain"],
        },
        "authored_by": "Dr. Smith",
    }
    create = orchestrator_client.put(
        f"/appointments/{appointment_id}/recap", json=payload
    )
    assert create.status_code == 200
    body = create.json()
    assert body["status"] == "draft"
    assert body["doctor_notes"] == "Continue plan"
    recap_id = body["id"]

    # PUT again with different notes is idempotent (same recap id).
    payload2 = dict(payload)
    payload2["doctor_notes"] = "Updated note"
    update = orchestrator_client.put(
        f"/appointments/{appointment_id}/recap", json=payload2
    )
    assert update.status_code == 200
    assert update.json()["id"] == recap_id
    assert update.json()["doctor_notes"] == "Updated note"

    # Preview returns a non-empty body and persists generated_text.
    preview = orchestrator_client.post(
        f"/appointments/{appointment_id}/recap/preview"
    )
    assert preview.status_code == 200
    body_text = preview.json()["body"]
    assert isinstance(body_text, str) and len(body_text) > 50

    fetched = orchestrator_client.get(
        f"/appointments/{appointment_id}/recap"
    ).json()
    assert fetched["generated_text"] == body_text

    # Send will fail at the gateway because we created a synthetic phone
    # number — but the endpoint should return 502 (not crash), and the
    # recap stays in draft so the doctor can retry.
    send = orchestrator_client.post(
        f"/appointments/{appointment_id}/recap/send"
    )
    assert send.status_code == 502


def test_recap_endpoint_404s_for_missing_appointment(orchestrator_client):
    response = orchestrator_client.put(
        "/appointments/999999999/recap",
        json={"doctor_notes": "x", "structured": {}},
    )
    assert response.status_code == 404

    response = orchestrator_client.post(
        "/appointments/999999999/recap/preview"
    )
    assert response.status_code == 404

    response = orchestrator_client.post(
        "/appointments/999999999/recap/send"
    )
    assert response.status_code == 404


async def test_recap_v2_template_injects_quick_reply_buttons(
    orchestrator_client, monkeypatch
):
    """When the recap template name is flipped to ``post_visit_recap_v2``
    (which has QUICK_REPLY buttons defined at Meta), the orchestrator's
    out-of-CSW send path must inject the dynamic ``recap-ack-{id}`` /
    ``recap-question-{id}`` button payloads so a tap routes back to the
    recap_handler with the recap_id intact. v1 (no buttons) was tested
    earlier in this file via the lock-after-send path; this test covers
    the v2 swap explicitly without depending on the default."""
    from services.orchestrator import main as orchestrator_main
    from app.db.models import AppointmentRecap, RecapStatus

    captured: dict = {}

    async def fake_gateway_send(payload: dict) -> str:
        # Capture the full payload that would have hit the gateway.
        captured.update(payload)
        return f"wamid.fake.{uuid.uuid4().hex[:8]}"

    # Stub the gateway HTTP call. We patch httpx.AsyncClient.post so we
    # see EXACTLY what the orchestrator sends — including the buttons
    # field that's the whole point of this test.
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"wamid": captured.get("_wamid")}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

        async def post(self, url: str, json: dict):  # noqa: A002
            captured.update(json)
            captured["_wamid"] = f"wamid.fake.{uuid.uuid4().hex[:8]}"
            return FakeResponse()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: FakeClient())
    monkeypatch.setattr(
        orchestrator_main, "_RECAP_TEMPLATE_NAME", "post_visit_recap_v2"
    )
    monkeypatch.setattr(
        orchestrator_main, "_FORCE_RECAP_TEMPLATE", True
    )

    appointment_id = await _seed_appointment()
    orchestrator_client.put(
        f"/appointments/{appointment_id}/recap",
        json={"doctor_notes": "v2 template test", "structured": {}},
    )
    sent = orchestrator_client.post(
        f"/appointments/{appointment_id}/recap/send"
    )
    assert sent.status_code == 200
    assert sent.json()["status"] == "sent"

    # v2 template path → buttons attached.
    assert captured.get("use_template") is True
    assert captured.get("template_name") == "post_visit_recap_v2"
    button_ids = {b["id"] for b in captured.get("buttons", [])}
    # Both id payloads carry the recap_id so a tap rounds back to the
    # recap_handler keyed correctly.
    assert any(bid.startswith("recap-ack-") for bid in button_ids)
    assert any(bid.startswith("recap-question-") for bid in button_ids)


async def test_recap_v1_template_skips_buttons(
    orchestrator_client, monkeypatch
):
    """Mirror of the v2 test for the v1 template — must NOT attach
    buttons because v1 has no QUICK_REPLY components defined at Meta
    and Meta would reject the send."""
    from services.orchestrator import main as orchestrator_main

    captured: dict = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"wamid": "wamid.fake.v1"}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

        async def post(self, url: str, json: dict):  # noqa: A002
            captured.update(json)
            return FakeResponse()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: FakeClient())
    monkeypatch.setattr(
        orchestrator_main, "_RECAP_TEMPLATE_NAME", "post_visit_recap_v1"
    )
    monkeypatch.setattr(
        orchestrator_main, "_FORCE_RECAP_TEMPLATE", True
    )

    appointment_id = await _seed_appointment()
    orchestrator_client.put(
        f"/appointments/{appointment_id}/recap",
        json={"doctor_notes": "v1 template test", "structured": {}},
    )
    sent = orchestrator_client.post(
        f"/appointments/{appointment_id}/recap/send"
    )
    assert sent.status_code == 200

    assert captured.get("template_name") == "post_visit_recap_v1"
    # No buttons on the v1 path — the dispatcher's _v2-suffix gate
    # didn't fire.
    assert "buttons" not in captured or captured["buttons"] == []


async def test_recap_locks_after_send(orchestrator_client, monkeypatch):
    """Once sent, PUT should 409 — drafts can only be edited before send."""
    from services.orchestrator import main as orchestrator_main

    appointment_id = await _seed_appointment()

    captured: dict = {}

    # Stub the gateway POST so send() succeeds without touching Meta.
    async def fake_send(**kwargs):
        captured.update(kwargs)
        return f"wamid.fake.{uuid.uuid4().hex[:8]}"

    monkeypatch.setattr(
        orchestrator_main, "_send_recap_via_gateway", fake_send
    )
    # Newly-seeded patient has no prior inbound → out-of-CSW. The send
    # path should still succeed (template branch) and the captured kwargs
    # should reflect that.


    orchestrator_client.put(
        f"/appointments/{appointment_id}/recap",
        json={
            "doctor_notes": "ok",
            "structured": {},
        },
    )
    sent = orchestrator_client.post(
        f"/appointments/{appointment_id}/recap/send"
    )
    assert sent.status_code == 200
    assert sent.json()["status"] == "sent"
    assert sent.json()["sent_message_id"].startswith("wamid.fake")
    # Synthetic patient has no prior inbound → must have hit the
    # template branch, not freeform.
    assert captured.get("in_csw") is False

    # PUT after send should be rejected.
    edit = orchestrator_client.put(
        f"/appointments/{appointment_id}/recap",
        json={"doctor_notes": "trying to edit", "structured": {}},
    )
    assert edit.status_code == 409

"""Two-person rule for patient erasure.

Default-off preserves single-step erasure. When ERASURE_DUAL_CONTROL=1 the
erase endpoint files a pending request instead of scrubbing; a DIFFERENT
operator must approve it, and a self-approval is rejected.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.db.models import Patient
from app.db.repositories import erasure_requests as er_repo
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
            full_name=f"Erase DC {suffix}",
            phone=f"erasedc-{suffix}",
            consent_sms=True,
        )
        db.add(p)
        await db.flush()
        await db.commit()
        return p.id, p.phone


def test_single_step_erasure_when_dual_control_off(
    orchestrator_client, monkeypatch
):
    monkeypatch.delenv("ERASURE_DUAL_CONTROL", raising=False)
    import asyncio

    pid, _ = asyncio.get_event_loop().run_until_complete(_seed_patient())
    r = orchestrator_client.post(
        f"/patients/{pid}/erase",
        json={"actor": "solo@clinic", "reason": "patient request", "confirm": True},
        headers={"X-Ops-Actor": "solo@clinic"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["erased_at"] is not None


def test_dual_control_files_request_then_approves(
    orchestrator_client, monkeypatch
):
    monkeypatch.setenv("ERASURE_DUAL_CONTROL", "1")
    import asyncio

    pid, _ = asyncio.get_event_loop().run_until_complete(_seed_patient())

    # Operator A files the request — 202, no scrub yet.
    r1 = orchestrator_client.post(
        f"/patients/{pid}/erase",
        json={"actor": "alice@clinic", "reason": "GDPR", "confirm": True},
        headers={"X-Ops-Actor": "alice@clinic"},
    )
    assert r1.status_code == 202, r1.text
    req_id = r1.json()["erasure_request_id"]

    # Patient not yet erased.
    SessionLocal = get_sessionmaker()

    async def _patient_erased() -> bool:
        async with SessionLocal() as db:
            p = await db.get(Patient, pid)
            return p.erased_at is not None

    assert asyncio.get_event_loop().run_until_complete(_patient_erased()) is False

    # Self-approval by the requester is rejected (403).
    r_self = orchestrator_client.post(
        f"/erasure-requests/{req_id}/approve",
        json={},
        headers={"X-Ops-Actor": "alice@clinic"},
    )
    assert r_self.status_code == 403, r_self.text

    # A DIFFERENT operator approves — scrub executes.
    r2 = orchestrator_client.post(
        f"/erasure-requests/{req_id}/approve",
        json={},
        headers={"X-Ops-Actor": "bob@clinic"},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["approved_by"] == "bob@clinic"
    assert asyncio.get_event_loop().run_until_complete(_patient_erased()) is True


def test_dual_control_duplicate_request_409(orchestrator_client, monkeypatch):
    monkeypatch.setenv("ERASURE_DUAL_CONTROL", "1")
    import asyncio

    pid, _ = asyncio.get_event_loop().run_until_complete(_seed_patient())
    body = {"actor": "alice@clinic", "reason": "GDPR", "confirm": True}
    headers = {"X-Ops-Actor": "alice@clinic"}
    r1 = orchestrator_client.post(f"/patients/{pid}/erase", json=body, headers=headers)
    assert r1.status_code == 202
    r2 = orchestrator_client.post(f"/patients/{pid}/erase", json=body, headers=headers)
    assert r2.status_code == 409, r2.text

    # Cleanup: reject the pending request so it doesn't linger.
    req_id = r1.json()["erasure_request_id"]
    orchestrator_client.post(
        f"/erasure-requests/{req_id}/reject",
        json={},
        headers={"X-Ops-Actor": "carol@clinic"},
    )


def test_pending_list_endpoint(orchestrator_client, monkeypatch):
    monkeypatch.setenv("ERASURE_DUAL_CONTROL", "1")
    import asyncio

    pid, _ = asyncio.get_event_loop().run_until_complete(_seed_patient())
    r = orchestrator_client.post(
        f"/patients/{pid}/erase",
        json={"actor": "dave@clinic", "reason": "x", "confirm": True},
        headers={"X-Ops-Actor": "dave@clinic"},
    )
    req_id = r.json()["erasure_request_id"]

    listing = orchestrator_client.get("/erasure-requests")
    assert listing.status_code == 200
    assert any(item["id"] == req_id for item in listing.json())

    # Cleanup
    orchestrator_client.post(
        f"/erasure-requests/{req_id}/reject",
        json={},
        headers={"X-Ops-Actor": "eve@clinic"},
    )

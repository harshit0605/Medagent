"""Integration tests for multi-patient households (DB-backed).

Skipped when DATABASE_URL is unset.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.db.models import Patient
from app.db.session import get_sessionmaker

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping household tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


async def _seed_patient(name: str) -> int:
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(
            full_name=f"{name} {suffix}",
            phone=f"hh-{suffix}",
            consent_sms=True,
        )
        db.add(p)
        await db.flush()
        await db.commit()
        return p.id


def test_create_household(orchestrator_client):
    resp = orchestrator_client.post(
        "/households",
        json={"name": "Sharma family", "primary_caregiver_phone": "+9199"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "Sharma family"
    assert body["members"] == []


async def test_add_members_one_caregiver_many_patients(orchestrator_client):
    p1 = await _seed_patient("Parent One")
    p2 = await _seed_patient("Parent Two")
    hh = orchestrator_client.post(
        "/households", json={"name": "Two-parent household"}
    ).json()
    hid = hh["id"]

    orchestrator_client.post(
        f"/households/{hid}/members", json={"patient_id": p1}
    )
    resp = orchestrator_client.post(
        f"/households/{hid}/members", json={"patient_id": p2}
    )
    assert resp.status_code == 200
    member_ids = {m["id"] for m in resp.json()["members"]}
    assert {p1, p2} <= member_ids  # both patients under one household

    # Each patient resolves back to the shared household.
    got = orchestrator_client.get(f"/patients/{p1}/household")
    assert got.status_code == 200
    assert got.json()["id"] == hid


async def test_add_member_unknown_returns_404(orchestrator_client):
    hh = orchestrator_client.post(
        "/households", json={"name": "Empty household"}
    ).json()
    resp = orchestrator_client.post(
        f"/households/{hh['id']}/members", json={"patient_id": 99999999}
    )
    assert resp.status_code == 404


async def test_patient_without_household_404(orchestrator_client):
    pid = await _seed_patient("Loner")
    resp = orchestrator_client.get(f"/patients/{pid}/household")
    assert resp.status_code == 404

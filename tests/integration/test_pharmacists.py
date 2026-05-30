"""Pharmacist registry + refill-ticket routing (E2, MVP #5)."""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.db.repositories import ops_tickets as ops_tickets_repo
from app.db.session import get_sessionmaker

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping integration tests",
)


@pytest.fixture(scope="module")
def client():
    from services.orchestrator.main import app

    with TestClient(app) as c:
        yield c


def test_create_list_deactivate_pharmacist(client):
    suffix = uuid.uuid4().hex[:6]
    r = client.post(
        "/pharmacists",
        json={
            "full_name": f"Priya Pharm {suffix}",
            "pharmacy_name": "Acme Rx",
            "phone": "+9199",
        },
    )
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    assert r.json()["active"] is True

    # Appears in the active list.
    listing = client.get("/pharmacists").json()
    assert any(p["id"] == pid for p in listing)

    # Deactivate → drops from default list, present in include_inactive.
    d = client.post(f"/pharmacists/{pid}/active", json={"active": False})
    assert d.status_code == 200
    assert d.json()["active"] is False
    assert all(p["id"] != pid for p in client.get("/pharmacists").json())
    assert any(
        p["id"] == pid
        for p in client.get("/pharmacists?include_inactive=true").json()
    )


def test_assign_refill_ticket_to_pharmacist(client):
    import asyncio

    suffix = uuid.uuid4().hex[:6]
    # Create a pharmacist.
    pid = client.post(
        "/pharmacists", json={"full_name": f"Sam Rx {suffix}"}
    ).json()["id"]

    # Seed a refill-help ticket.
    SessionLocal = get_sessionmaker()

    async def _seed_ticket() -> int:
        async with SessionLocal() as db:
            t = await ops_tickets_repo.create(
                db,
                patient_id=f"rx-route-{suffix}",
                category="refill_help",
                priority="medium",
                sla_minutes=240,
                notes="needs refill",
            )
            await db.commit()
            return t.id

    tid = asyncio.get_event_loop().run_until_complete(_seed_ticket())

    r = client.post(
        f"/ops/tickets/{tid}/assign-pharmacist",
        json={"pharmacist_id": pid},
        headers={"X-Ops-Actor": "ops.alice"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["pharmacist_id"] == pid
    assert f"pharmacist:{pid}" in r.json()["assigned_to"]


def test_assign_to_unknown_pharmacist_404(client):
    import asyncio

    SessionLocal = get_sessionmaker()

    async def _seed_ticket() -> int:
        async with SessionLocal() as db:
            t = await ops_tickets_repo.create(
                db,
                patient_id=f"rx-404-{uuid.uuid4().hex[:6]}",
                category="refill_help",
                priority="medium",
                sla_minutes=240,
            )
            await db.commit()
            return t.id

    tid = asyncio.get_event_loop().run_until_complete(_seed_ticket())
    r = client.post(
        f"/ops/tickets/{tid}/assign-pharmacist",
        json={"pharmacist_id": 99999999},
    )
    assert r.status_code == 404

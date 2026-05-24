"""Integration test for GET /patients (the ops-console listing).

Previously untested. Guards the open-ticket-count behavior after the N+1
fix: the count comes from a single grouped query (open_counts_by_patient)
keyed by phone, and must count only OPEN tickets.

Skipped when DATABASE_URL is unset.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.db.models import Patient
from app.db.repositories import ops_tickets as ops_tickets_repo
from app.db.session import get_sessionmaker

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping patients-list integration test",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


async def _seed_patient_with_tickets() -> tuple[int, str]:
    """One patient with two OPEN tickets + one RESOLVED ticket (by phone)."""
    suffix = uuid.uuid4().hex[:8]
    phone = f"list-test-{suffix}"
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(full_name=f"List Test {suffix}", phone=phone)
        db.add(p)
        await db.flush()
        await ops_tickets_repo.create(
            db, patient_id=phone, category="triage", priority="p1",
            sla_minutes=60, notes="x",
        )
        await ops_tickets_repo.create(
            db, patient_id=phone, category="refill_help", priority="p2",
            sla_minutes=60, notes="x",
        )
        resolved = await ops_tickets_repo.create(
            db, patient_id=phone, category="lab_help", priority="p2",
            sla_minutes=60, notes="x",
        )
        await db.flush()
        await ops_tickets_repo.resolve(db, resolved.id, actor="ops")
        await db.commit()
        return p.id, phone


def test_patients_list_open_ticket_count(orchestrator_client):
    import asyncio

    pid, _phone = asyncio.get_event_loop().run_until_complete(
        _seed_patient_with_tickets()
    )

    r = orchestrator_client.get("/patients", params={"limit": 500})
    assert r.status_code == 200
    rows = r.json()
    row = next((x for x in rows if x["id"] == pid), None)
    assert row is not None, "seeded patient missing from /patients"
    # Two open, one resolved → 2 (resolved excluded).
    assert row["open_ticket_count"] == 2

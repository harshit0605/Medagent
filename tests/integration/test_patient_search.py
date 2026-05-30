"""Operator full-text patient search (F3)."""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.db.models import Patient, Regimen
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


async def _seed(*, name: str, phone: str, med: str | None = None) -> int:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(full_name=name, phone=phone, consent_sms=True)
        db.add(p)
        await db.flush()
        if med:
            db.add(
                Regimen(
                    patient_id=p.id,
                    medication_name=med,
                    dose="1 tab",
                    schedule={"type": "times_of_day", "times": ["08:00"]},
                )
            )
        await db.commit()
        return p.id


def test_search_by_name(client):
    token = uuid.uuid4().hex[:8]
    import asyncio

    pid = asyncio.get_event_loop().run_until_complete(
        _seed(name=f"Ramesh Kumar {token}", phone=f"srch-{token}")
    )
    r = client.get(f"/patients/search?q=Ramesh Kumar {token}")
    assert r.status_code == 200, r.text
    assert any(row["id"] == pid for row in r.json())


def test_search_by_phone_fragment(client):
    token = uuid.uuid4().hex[:8]
    import asyncio

    pid = asyncio.get_event_loop().run_until_complete(
        _seed(name=f"Phone Person {token}", phone=f"srchphone-{token}")
    )
    r = client.get(f"/patients/search?q=srchphone-{token}")
    assert r.status_code == 200
    assert any(row["id"] == pid for row in r.json())


def test_search_by_medication(client):
    token = uuid.uuid4().hex[:8]
    import asyncio

    pid = asyncio.get_event_loop().run_until_complete(
        _seed(
            name=f"Med Patient {token}",
            phone=f"srchmed-{token}",
            med=f"Zorbitol{token}",
        )
    )
    r = client.get(f"/patients/search?q=Zorbitol{token}")
    assert r.status_code == 200
    assert any(row["id"] == pid for row in r.json())


def test_search_empty_query_returns_empty(client):
    r = client.get("/patients/search?q=")
    assert r.status_code == 200
    assert r.json() == []


def test_search_path_does_not_collide_with_detail(client):
    # /patients/search must route to search, not be parsed as patient id.
    r = client.get("/patients/search?q=nonexistent-xyz-123")
    assert r.status_code == 200
    assert isinstance(r.json(), list)

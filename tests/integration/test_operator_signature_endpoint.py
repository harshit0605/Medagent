"""Integration tests for the HMAC-signed X-Ops-Actor flow.

Verifies the orchestrator-side ``resolve_actor`` behaviour on a real
privileged endpoint (DSAR export):

  * Default (key unset): accepts unsigned header; audit row records signed=False.
  * Key set, required=0: accepts signed header → signed=True; mis-signed
    still accepted (signed=False).
  * Key set, required=1: unsigned → 401; mis-signed → 401; signed → 200.

Skipped when DATABASE_URL is unset.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.db.models import Patient
from app.db.repositories import operator_actions as ops_audit
from app.db.session import get_sessionmaker
from app.operator_signature import sign

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping integration tests",
)


_TEST_KEY = "test-signing-key-deadbeef"


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


@pytest.fixture
def signing_enabled(monkeypatch):
    """Configure the signing key but leave REQUIRED off."""
    monkeypatch.setenv("OPS_ACTOR_SIGNING_KEY", _TEST_KEY)
    monkeypatch.delenv("OPS_ACTOR_SIGNATURE_REQUIRED", raising=False)
    yield _TEST_KEY


@pytest.fixture
def signing_strict(monkeypatch):
    """Configure the signing key AND require signatures."""
    monkeypatch.setenv("OPS_ACTOR_SIGNING_KEY", _TEST_KEY)
    monkeypatch.setenv("OPS_ACTOR_SIGNATURE_REQUIRED", "1")
    yield _TEST_KEY


async def _seed_patient() -> int:
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(
            full_name=f"Signed Actor Test {suffix}",
            phone=f"sigtest-{suffix}",
            consent_sms=True,
        )
        db.add(p)
        await db.flush()
        await db.commit()
        return p.id


async def _latest_export_audit(patient_id: int):
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows = await ops_audit.list_for_target(
            db, target_type="patient", target_id=patient_id
        )
    return next(
        (r for r in rows if r.action == ops_audit.ACTION_PATIENT_EXPORT),
        None,
    )


def test_export_accepts_signed_actor_when_key_configured(
    orchestrator_client, signing_enabled
):
    import asyncio

    pid = asyncio.get_event_loop().run_until_complete(_seed_patient())
    actor = "alice@clinic"
    r = orchestrator_client.get(
        f"/patients/{pid}/export?window_days=7",
        headers={
            "X-Ops-Actor": actor,
            "X-Ops-Actor-Signature": sign(actor, key=signing_enabled),
        },
    )
    assert r.status_code == 200, r.text
    row = asyncio.get_event_loop().run_until_complete(
        _latest_export_audit(pid)
    )
    assert row is not None
    assert row.operator_id == actor
    assert row.details.get("signed") is True


def test_export_accepts_unsigned_actor_when_required_off(
    orchestrator_client, signing_enabled
):
    """REQUIRED off + signing key set: unsigned still works,
    signed=False stamped on the audit row."""
    import asyncio

    pid = asyncio.get_event_loop().run_until_complete(_seed_patient())
    r = orchestrator_client.get(
        f"/patients/{pid}/export?window_days=7",
        headers={"X-Ops-Actor": "alice@clinic"},  # no signature
    )
    assert r.status_code == 200, r.text
    row = asyncio.get_event_loop().run_until_complete(
        _latest_export_audit(pid)
    )
    assert row is not None
    assert row.details.get("signed") is False


def test_export_rejects_unsigned_when_required(
    orchestrator_client, signing_strict
):
    import asyncio

    pid = asyncio.get_event_loop().run_until_complete(_seed_patient())
    r = orchestrator_client.get(
        f"/patients/{pid}/export?window_days=7",
        headers={"X-Ops-Actor": "alice@clinic"},
    )
    assert r.status_code == 401, r.text


def test_export_rejects_mis_signed_when_required(
    orchestrator_client, signing_strict
):
    import asyncio

    pid = asyncio.get_event_loop().run_until_complete(_seed_patient())
    actor = "alice@clinic"
    r = orchestrator_client.get(
        f"/patients/{pid}/export?window_days=7",
        headers={
            "X-Ops-Actor": actor,
            "X-Ops-Actor-Signature": sign(actor, key="wrong-key"),
        },
    )
    assert r.status_code == 401, r.text


def test_export_accepts_correctly_signed_when_required(
    orchestrator_client, signing_strict
):
    import asyncio

    pid = asyncio.get_event_loop().run_until_complete(_seed_patient())
    actor = "alice@clinic"
    r = orchestrator_client.get(
        f"/patients/{pid}/export?window_days=7",
        headers={
            "X-Ops-Actor": actor,
            "X-Ops-Actor-Signature": sign(actor, key=signing_strict),
        },
    )
    assert r.status_code == 200, r.text
    row = asyncio.get_event_loop().run_until_complete(
        _latest_export_audit(pid)
    )
    assert row is not None
    assert row.details.get("signed") is True

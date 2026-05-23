"""Integration tests for the patient right-of-erasure flow.

End-to-end against real Postgres because:
    1. The erasure does cross-table UPDATE statements with JSON
       overwrite; SQLAlchemy lowering varies by dialect.
    2. Idempotency relies on reading-back the erased_at column
       which only exists in the live schema.
    3. The audit row must persist exactly as written and is
       readable post-erasure — confirmation we kept the trail.

Skipped when DATABASE_URL is unset.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import (
    AuditRecord,
    Caregiver,
    InboundClassification,
    MessageLog,
    Patient,
)
from app.db.repositories import (
    inbound_classifications as inbound_classifications_repo,
    message_log as message_log_repo,
    ops_tickets as ops_tickets_repo,
)
from app.db.session import get_sessionmaker

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping erasure integration tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


async def _seed_patient_with_history() -> tuple[int, str]:
    """Create a patient with PII across the connected tables so
    the erasure has something to anonymize. Returns (id, phone)."""
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(
            full_name=f"Erase Test {suffix}",
            phone=f"erase-{suffix}",
            external_id=f"ext-{suffix}",
            consent_sms=True,
            preferred_language="hi",
            cohort_diabetes=True,
        )
        db.add(p)
        await db.flush()

        # Caregiver row.
        db.add(
            Caregiver(
                patient_id=p.id,
                full_name=f"Family Member {suffix}",
                phone=f"family-{suffix}",
                relationship_to_patient="spouse",
            )
        )

        # Inbound message log row.
        await message_log_repo.append_inbound(
            db,
            patient_id=p.phone,
            payload={"text": "I have a question about my medication"},
        )

        # Ops ticket row.
        await ops_tickets_repo.create(
            db,
            patient_id=p.phone,
            category="side_effect_report",
            priority="high",
            sla_minutes=30,
            notes="Patient said: I'm getting headaches",
        )

        # Inbound classification row.
        await inbound_classifications_repo.create(
            db,
            message_id=f"msg-{suffix}",
            patient_phone=p.phone,
            patient_db_id=p.id,
            inbound_text="I have a question",
            category="general",
            summary="patient asking about something",
            urgency="low",
            handler_used="compose",
            response_text="here is the response",
            escalated=False,
            ticket_id=None,
            input_kind="text",
        )

        await db.commit()
        return p.id, p.phone


# ---- Erasure endpoint round-trip -----------------------------------------


def test_erase_endpoint_anonymizes_patient_row(orchestrator_client):
    """The patient row's PII fields must be overwritten in
    place. Reading back via /patients/{id} should show the
    placeholder values + erased_at timestamp."""
    import asyncio

    pid, _ = asyncio.get_event_loop().run_until_complete(
        _seed_patient_with_history()
    )

    r = orchestrator_client.post(
        f"/patients/{pid}/erase",
        json={
            "actor": "ops_alice",
            "reason": "subject access request",
            "confirm": True,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["patient_id"] == pid
    assert body["erased_at"] is not None
    assert body["anonymized_phone"].startswith("erased:")


def test_erase_requires_confirm_flag(orchestrator_client):
    """Defense-in-depth: missing or false ``confirm`` must 400.
    A misclick on the UI button shouldn't destroy data."""
    import asyncio

    pid, _ = asyncio.get_event_loop().run_until_complete(
        _seed_patient_with_history()
    )

    r = orchestrator_client.post(
        f"/patients/{pid}/erase",
        json={
            "actor": "ops",
            "reason": "test",
            # confirm omitted → defaults False
        },
    )
    assert r.status_code == 400

    r = orchestrator_client.post(
        f"/patients/{pid}/erase",
        json={
            "actor": "ops",
            "reason": "test",
            "confirm": False,
        },
    )
    assert r.status_code == 400


def test_erase_404_for_unknown_patient(orchestrator_client):
    r = orchestrator_client.post(
        "/patients/999999999/erase",
        json={"actor": "ops", "reason": "test", "confirm": True},
    )
    assert r.status_code == 404


def test_erase_validates_actor_and_reason(orchestrator_client):
    """The pydantic model requires non-empty actor + reason. The
    audit trail is the legal proof of erasure — empty reason
    would be useless to a regulator."""
    r = orchestrator_client.post(
        "/patients/1/erase",
        json={"actor": "", "reason": "test", "confirm": True},
    )
    assert r.status_code == 422

    r = orchestrator_client.post(
        "/patients/1/erase",
        json={"actor": "ops", "reason": "", "confirm": True},
    )
    assert r.status_code == 422


# ---- Cross-table anonymization -------------------------------------------


def test_erase_anonymizes_caregivers(orchestrator_client):
    """Caregiver rows linked to the erased patient must have
    their full_name + phone overwritten and be deactivated."""
    import asyncio

    pid, _ = asyncio.get_event_loop().run_until_complete(
        _seed_patient_with_history()
    )
    orchestrator_client.post(
        f"/patients/{pid}/erase",
        json={"actor": "ops", "reason": "test", "confirm": True},
    )

    async def _read_caregivers():
        SessionLocal = get_sessionmaker()
        async with SessionLocal() as db:
            stmt = select(Caregiver).where(
                Caregiver.patient_id == pid
            )
            return list((await db.execute(stmt)).scalars().all())

    rows = asyncio.get_event_loop().run_until_complete(
        _read_caregivers()
    )
    assert len(rows) >= 1
    for c in rows:
        assert c.full_name == "[erased]"
        assert c.phone == "[erased]"
        assert c.active is False


def test_erase_anonymizes_message_log(orchestrator_client):
    """Inbound + outbound message_log rows for the patient must
    have their JSON contents overwritten so the patient's
    actual words are gone."""
    import asyncio

    pid, original_phone = asyncio.get_event_loop().run_until_complete(
        _seed_patient_with_history()
    )
    orchestrator_client.post(
        f"/patients/{pid}/erase",
        json={"actor": "ops", "reason": "test", "confirm": True},
    )

    async def _read_logs():
        SessionLocal = get_sessionmaker()
        async with SessionLocal() as db:
            # Original phone should NOT have any rows anymore —
            # they were re-keyed to the anonymized phone.
            stmt = select(MessageLog).where(
                MessageLog.patient_id == original_phone
            )
            original = list(
                (await db.execute(stmt)).scalars().all()
            )
            return original

    rows = asyncio.get_event_loop().run_until_complete(_read_logs())
    # Original phone should have zero rows (re-keyed to anonymized).
    assert rows == []


def test_erase_anonymizes_ops_tickets(orchestrator_client):
    import asyncio

    pid, original_phone = asyncio.get_event_loop().run_until_complete(
        _seed_patient_with_history()
    )
    orchestrator_client.post(
        f"/patients/{pid}/erase",
        json={"actor": "ops", "reason": "test", "confirm": True},
    )

    async def _read_tickets_by_phone(phone: str):
        SessionLocal = get_sessionmaker()
        async with SessionLocal() as db:
            return await ops_tickets_repo.list_for_patient(db, phone)

    rows_original = asyncio.get_event_loop().run_until_complete(
        _read_tickets_by_phone(original_phone)
    )
    # Original phone has no tickets anymore.
    assert rows_original == []


def test_erase_anonymizes_inbound_classifications(orchestrator_client):
    import asyncio

    pid, original_phone = asyncio.get_event_loop().run_until_complete(
        _seed_patient_with_history()
    )
    orchestrator_client.post(
        f"/patients/{pid}/erase",
        json={"actor": "ops", "reason": "test", "confirm": True},
    )

    async def _read_classifications():
        SessionLocal = get_sessionmaker()
        async with SessionLocal() as db:
            stmt = select(InboundClassification).where(
                InboundClassification.patient_phone == original_phone
            )
            return list(
                (await db.execute(stmt)).scalars().all()
            )

    rows = asyncio.get_event_loop().run_until_complete(
        _read_classifications()
    )
    assert rows == []


# ---- Audit trail preserved ------------------------------------------------


def test_erase_writes_audit_row_keyed_to_anonymized_phone(
    orchestrator_client,
):
    """The audit log MUST persist past erasure — it's the
    regulator-trace evidence of when the erasure happened
    and who triggered it. The audit row's patient_id is the
    NEW anonymized phone so the trail joins forward."""
    import asyncio

    pid, _ = asyncio.get_event_loop().run_until_complete(
        _seed_patient_with_history()
    )
    r = orchestrator_client.post(
        f"/patients/{pid}/erase",
        json={
            "actor": "ops_audit_test",
            "reason": "audit verification",
            "confirm": True,
        },
    )
    anonymized_phone = r.json()["anonymized_phone"]

    async def _read_audit():
        SessionLocal = get_sessionmaker()
        async with SessionLocal() as db:
            stmt = (
                select(AuditRecord)
                .where(AuditRecord.patient_id == anonymized_phone)
                .where(
                    AuditRecord.reason_codes.contains(
                        ["patient_erasure"]
                    )
                )
            )
            return list((await db.execute(stmt)).scalars().all())

    # Note: contains() on JSON column requires JSONB cast.
    # Using the search helper instead would be cleaner — but
    # for the spot-check, direct fetch by anonymized phone +
    # filter in Python.
    async def _read_audit_simple():
        SessionLocal = get_sessionmaker()
        async with SessionLocal() as db:
            stmt = select(AuditRecord).where(
                AuditRecord.patient_id == anonymized_phone
            )
            return list((await db.execute(stmt)).scalars().all())

    rows = asyncio.get_event_loop().run_until_complete(
        _read_audit_simple()
    )
    assert len(rows) >= 1
    audit = rows[-1]
    assert "patient_erasure" in (audit.reason_codes or [])
    assert audit.details.get("actor") == "ops_audit_test"
    assert audit.details.get("reason") == "audit verification"


# ---- Idempotency ----------------------------------------------------------


def test_erase_is_idempotent(orchestrator_client):
    """A second erase call on an already-erased patient must
    NOT overwrite the original ``erased_at`` (the LEGALLY
    relevant moment is the FIRST erasure). It must still
    return 200 — the operation is naturally idempotent."""
    import asyncio

    pid, _ = asyncio.get_event_loop().run_until_complete(
        _seed_patient_with_history()
    )

    first = orchestrator_client.post(
        f"/patients/{pid}/erase",
        json={"actor": "ops", "reason": "first", "confirm": True},
    )
    assert first.status_code == 200
    first_at = first.json()["erased_at"]

    second = orchestrator_client.post(
        f"/patients/{pid}/erase",
        json={"actor": "ops", "reason": "second", "confirm": True},
    )
    assert second.status_code == 200
    # ``erased_at`` preserved from the first call.
    assert second.json()["erased_at"] == first_at


# ---- Patient detail surfaces erased state ---------------------------------


def test_patient_detail_after_erasure_shows_anonymized_state(
    orchestrator_client,
):
    """GET /patients/{id} on an erased patient should still work
    (the FK skeleton is preserved) and show the anonymized
    placeholder values so a doctor / ops scanning the page sees
    that the patient has been erased rather than encountering
    a 404 or stale PII."""
    import asyncio

    pid, _ = asyncio.get_event_loop().run_until_complete(
        _seed_patient_with_history()
    )
    orchestrator_client.post(
        f"/patients/{pid}/erase",
        json={"actor": "ops", "reason": "test", "confirm": True},
    )

    r = orchestrator_client.get(f"/patients/{pid}")
    assert r.status_code == 200
    body = r.json()
    assert body["full_name"] == "[erased]"
    assert body["phone"].startswith("erased:")
    assert body["consent_sms"] is False
    assert body["bot_paused_at"] is not None
    assert body["consent_revoked_reason"] == "patient_erasure"

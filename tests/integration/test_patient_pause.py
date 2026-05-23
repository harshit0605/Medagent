"""Integration tests for the patient bot-pause endpoint and the
dispatcher's pause gate.

Three layers of coverage:

    1. Repo layer — pause_bot / unpause_bot stamp + clear the audit
       columns idempotently against a real Postgres row.
    2. API layer — POST /patients/{id}/pause-bot + unpause-bot
       round-trip through the orchestrator.
    3. Patient detail — GET /patients/{id} surfaces the pause state
       so the ops console UI can render its indicator.

Skipped when DATABASE_URL is unset so CI without Postgres still
passes.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.db.models import Patient
from app.db.repositories import patients as patients_repo
from app.db.session import get_sessionmaker

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping pause integration tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


async def _create_patient() -> int:
    """Create a unique patient and return its id."""
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(
            full_name=f"Pause Test {suffix}",
            phone=f"pause-test-{suffix}",
            consent_sms=True,
        )
        db.add(p)
        await db.flush()
        await db.commit()
        return p.id


# ---- Repo layer ----------------------------------------------------------


async def test_pause_bot_stamps_audit_columns():
    """``pause_bot(actor, reason)`` writes all three audit columns
    on a fresh patient. ``consent_sms`` must remain unchanged —
    that's what makes pause distinct from opt-out."""
    pid = await _create_patient()
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        row = await patients_repo.pause_bot(
            db, pid, actor="alice", reason="complaint received"
        )
        await db.commit()
        assert row is not None
        assert row.bot_paused_at is not None
        assert row.bot_paused_by == "alice"
        assert row.bot_paused_reason == "complaint received"
        # Consent unchanged — the whole point of this column set.
        assert row.consent_sms is True


async def test_pause_bot_idempotent_preserves_original_timestamp():
    """A second pause on an already-paused patient must NOT
    overwrite the original timestamp / actor — those record when
    the pause actually started. Reason is allowed to update so a
    follow-up "still investigating" can supersede the initial
    reason without losing the moment of pause."""
    pid = await _create_patient()
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        first = await patients_repo.pause_bot(
            db, pid, actor="alice", reason="initial"
        )
        await db.commit()
        original_at = first.bot_paused_at
        original_by = first.bot_paused_by

        # Second pause with a different actor + reason.
        second = await patients_repo.pause_bot(
            db, pid, actor="bob", reason="still investigating"
        )
        await db.commit()
        assert second.bot_paused_at == original_at  # original moment preserved
        assert second.bot_paused_by == original_by  # original actor preserved
        assert second.bot_paused_reason == "still investigating"  # reason updated


async def test_unpause_bot_clears_all_audit_columns():
    """``unpause_bot`` must clear ALL three columns. Stale
    ``bot_paused_reason`` after unpause would mislead future ops
    sessions reading the timeline."""
    pid = await _create_patient()
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await patients_repo.pause_bot(
            db, pid, actor="ops", reason="testing"
        )
        await db.commit()
        cleared = await patients_repo.unpause_bot(db, pid)
        await db.commit()
        assert cleared.bot_paused_at is None
        assert cleared.bot_paused_reason is None
        assert cleared.bot_paused_by is None


async def test_pause_unpause_unknown_patient_returns_none():
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        assert (
            await patients_repo.pause_bot(
                db, 999999999, actor="ops", reason="test"
            )
        ) is None
        assert (
            await patients_repo.unpause_bot(db, 999999999)
        ) is None


# ---- API layer -----------------------------------------------------------


def test_pause_endpoint_round_trip(orchestrator_client):
    """End-to-end POST /pause-bot → patient detail surfaces the
    pause. POST /unpause-bot → state clears."""
    import asyncio

    pid = asyncio.get_event_loop().run_until_complete(_create_patient())

    # Pause.
    r = orchestrator_client.post(
        f"/patients/{pid}/pause-bot",
        json={"actor": "alice", "reason": "endpoint test"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["bot_paused_at"] is not None
    assert body["bot_paused_by"] == "alice"
    assert body["bot_paused_reason"] == "endpoint test"

    # Detail re-read confirms persistence.
    detail = orchestrator_client.get(f"/patients/{pid}")
    assert detail.status_code == 200
    assert detail.json()["bot_paused_at"] is not None

    # Unpause.
    r = orchestrator_client.post(f"/patients/{pid}/unpause-bot")
    assert r.status_code == 200
    body = r.json()
    assert body["bot_paused_at"] is None
    assert body["bot_paused_reason"] is None
    assert body["bot_paused_by"] is None


def test_pause_endpoint_404_for_unknown_patient(orchestrator_client):
    r = orchestrator_client.post(
        "/patients/999999999/pause-bot",
        json={"actor": "ops", "reason": "test"},
    )
    assert r.status_code == 404


def test_pause_endpoint_validates_required_fields(orchestrator_client):
    """The pydantic model requires ``actor`` and ``reason`` (both
    min_length=1) — missing or empty inputs should 422 not 500.
    Without this guard, an empty reason would land in the audit
    column and a future "why did we pause?" review would show
    nothing."""
    import asyncio

    pid = asyncio.get_event_loop().run_until_complete(_create_patient())

    r = orchestrator_client.post(
        f"/patients/{pid}/pause-bot",
        json={"actor": "ops"},  # missing reason
    )
    assert r.status_code == 422

    r = orchestrator_client.post(
        f"/patients/{pid}/pause-bot",
        json={"actor": "", "reason": "test"},  # empty actor
    )
    assert r.status_code == 422


def test_patient_detail_includes_pause_columns(orchestrator_client):
    """Pause columns must surface on the existing detail endpoint
    so the ops console UI doesn't need a second fetch to render
    its pause indicator."""
    import asyncio

    pid = asyncio.get_event_loop().run_until_complete(_create_patient())
    r = orchestrator_client.get(f"/patients/{pid}")
    assert r.status_code == 200
    body = r.json()
    # Always present, NULL when not paused.
    assert "bot_paused_at" in body
    assert "bot_paused_reason" in body
    assert "bot_paused_by" in body
    assert body["bot_paused_at"] is None

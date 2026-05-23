"""Integration tests for the bot-reply quality feedback flow.

End-to-end against real Postgres because the new columns + repo
helpers + endpoints all sit on the live schema. Skipped when
DATABASE_URL is unset.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.db.repositories import (
    inbound_classifications as inbound_classifications_repo,
)
from app.db.session import get_sessionmaker

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping inbox feedback tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


async def _seed_classification() -> int:
    """Create one classification row and return its id."""
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        row = await inbound_classifications_repo.create(
            db,
            message_id=f"msg-{suffix}",
            patient_phone=f"feedback-{suffix}",
            patient_db_id=None,
            inbound_text="hello",
            category="general",
            summary="patient said hello",
            urgency="low",
            handler_used="compose",
            response_text="hi there",
            escalated=False,
            ticket_id=None,
            input_kind="text",
        )
        await db.commit()
        return row.id


# ---- Repo-level set/clear ------------------------------------------------


async def test_set_feedback_stamps_all_columns():
    cid = await _seed_classification()
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        row = await inbound_classifications_repo.set_feedback(
            db,
            cid,
            rating=1,
            actor="ops_alice",
            note="bot got this right",
        )
        await db.commit()
        assert row.feedback_rating == 1
        assert row.feedback_by == "ops_alice"
        assert row.feedback_note == "bot got this right"
        assert row.feedback_at is not None


async def test_set_feedback_overwrites_prior_rating():
    cid = await _seed_classification()
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await inbound_classifications_repo.set_feedback(
            db, cid, rating=1, actor="ops"
        )
        await db.commit()
        row = await inbound_classifications_repo.set_feedback(
            db, cid, rating=-1, actor="doctor"
        )
        await db.commit()
        # Latest rating wins; we don't keep history in v1.
        assert row.feedback_rating == -1
        assert row.feedback_by == "doctor"


async def test_set_feedback_rejects_invalid_rating():
    cid = await _seed_classification()
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        with pytest.raises(ValueError):
            await inbound_classifications_repo.set_feedback(
                db, cid, rating=0, actor="ops"
            )
        with pytest.raises(ValueError):
            await inbound_classifications_repo.set_feedback(
                db, cid, rating=2, actor="ops"
            )


async def test_clear_feedback_nulls_all_columns():
    cid = await _seed_classification()
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await inbound_classifications_repo.set_feedback(
            db, cid, rating=1, actor="ops", note="good"
        )
        await db.commit()
        cleared = await inbound_classifications_repo.clear_feedback(
            db, cid
        )
        await db.commit()
        assert cleared.feedback_rating is None
        assert cleared.feedback_by is None
        assert cleared.feedback_note is None
        assert cleared.feedback_at is None


async def test_set_feedback_unknown_returns_none():
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        out = await inbound_classifications_repo.set_feedback(
            db, 999999999, rating=1, actor="ops"
        )
        assert out is None


# ---- HTTP endpoint round-trip --------------------------------------------


def test_endpoint_round_trip(orchestrator_client):
    import asyncio

    cid = asyncio.get_event_loop().run_until_complete(
        _seed_classification()
    )

    # POST a thumbs-up.
    r = orchestrator_client.post(
        f"/ops/inbox/{cid}/feedback",
        json={"rating": 1, "actor": "ops_alice", "note": "good"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["feedback_rating"] == 1
    assert body["feedback_by"] == "ops_alice"
    assert body["feedback_note"] == "good"
    assert body["feedback_at"] is not None

    # POST a thumbs-down (overwrites).
    r = orchestrator_client.post(
        f"/ops/inbox/{cid}/feedback",
        json={"rating": -1, "actor": "doctor"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["feedback_rating"] == -1
    assert body["feedback_by"] == "doctor"

    # DELETE clears.
    r = orchestrator_client.delete(f"/ops/inbox/{cid}/feedback")
    assert r.status_code == 200
    body = r.json()
    assert body["feedback_rating"] is None


def test_endpoint_validates_rating(orchestrator_client):
    """The Literal[-1, 1] in the request schema rejects out-of-
    range ratings at the validation layer."""
    import asyncio

    cid = asyncio.get_event_loop().run_until_complete(
        _seed_classification()
    )

    r = orchestrator_client.post(
        f"/ops/inbox/{cid}/feedback",
        json={"rating": 0, "actor": "ops"},
    )
    assert r.status_code == 422

    r = orchestrator_client.post(
        f"/ops/inbox/{cid}/feedback",
        json={"rating": 5, "actor": "ops"},
    )
    assert r.status_code == 422


def test_endpoint_404_for_unknown_classification(orchestrator_client):
    r = orchestrator_client.post(
        "/ops/inbox/999999999/feedback",
        json={"rating": 1, "actor": "ops"},
    )
    assert r.status_code == 404

    r = orchestrator_client.delete("/ops/inbox/999999999/feedback")
    assert r.status_code == 404


def test_endpoint_validates_actor(orchestrator_client):
    """Missing or empty ``actor`` must 422 — the audit trail is
    the whole point of the feedback row."""
    import asyncio

    cid = asyncio.get_event_loop().run_until_complete(
        _seed_classification()
    )
    r = orchestrator_client.post(
        f"/ops/inbox/{cid}/feedback",
        json={"rating": 1, "actor": ""},
    )
    assert r.status_code == 422


def test_inbox_list_surfaces_feedback_fields(orchestrator_client):
    """GET /ops/inbox must include the feedback fields so the UI
    renders the existing rating without a second fetch."""
    import asyncio

    cid = asyncio.get_event_loop().run_until_complete(
        _seed_classification()
    )

    orchestrator_client.post(
        f"/ops/inbox/{cid}/feedback",
        json={"rating": 1, "actor": "ops"},
    )

    r = orchestrator_client.get("/ops/inbox", params={"limit": 200})
    assert r.status_code == 200
    rows = r.json()
    target = next((row for row in rows if row["id"] == cid), None)
    assert target is not None
    assert target["feedback_rating"] == 1
    assert target["feedback_by"] == "ops"

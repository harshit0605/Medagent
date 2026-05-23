"""Integration tests for the production-observability /ops/health
endpoint + the underlying service_heartbeats repo.

Covers:
- Repo upsert: first record creates, subsequent ones update.
- Consecutive-error counter increments on repeated errors and resets
  on the first non-error outcome.
- Endpoint reflects heartbeat freshness via per-component staleness
  thresholds.
- Endpoint surfaces failed_events_24h + pending_overdue counters.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.db.models import (
    ScheduledEvent,
    ScheduledEventStatus,
)
from app.db.repositories import service_heartbeats as service_heartbeats_repo
from app.db.session import get_sessionmaker

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping ops/health integration tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


# ---- Repo --------------------------------------------------------------


async def test_record_creates_then_updates_single_row():
    """Heartbeat repo is single-row-per-component (PK is the name).
    First record creates the row; subsequent records update in place."""
    component = f"test.heartbeat.{uuid.uuid4().hex[:8]}"

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        first = await service_heartbeats_repo.record(
            db,
            component=component,
            outcome="ok",
            details={"dispatched": 3},
        )
        await db.commit()
    assert first.last_outcome == "ok"
    assert first.consecutive_errors == 0
    assert first.details == {"dispatched": 3}

    async with SessionLocal() as db:
        second = await service_heartbeats_repo.record(
            db,
            component=component,
            outcome="ok",
            details={"dispatched": 5},
        )
        await db.commit()
    # Same row (no auto-incrementing id, PK is component).
    assert second.component == component
    assert second.details == {"dispatched": 5}
    # updated_at moved forward.
    assert second.updated_at >= first.updated_at


async def test_consecutive_error_counter_increments_then_resets():
    component = f"test.err.{uuid.uuid4().hex[:8]}"
    SessionLocal = get_sessionmaker()

    async with SessionLocal() as db:
        await service_heartbeats_repo.record(
            db, component=component, outcome="error"
        )
        await db.commit()
        row = await service_heartbeats_repo.get(db, component)
    assert row.consecutive_errors == 1

    async with SessionLocal() as db:
        await service_heartbeats_repo.record(
            db, component=component, outcome="error"
        )
        await db.commit()
        row = await service_heartbeats_repo.get(db, component)
    assert row.consecutive_errors == 2

    # First non-error outcome resets the counter.
    async with SessionLocal() as db:
        await service_heartbeats_repo.record(
            db, component=component, outcome="ok"
        )
        await db.commit()
        row = await service_heartbeats_repo.get(db, component)
    assert row.consecutive_errors == 0


# ---- Endpoint ----------------------------------------------------------


async def test_health_endpoint_includes_recorded_components(
    orchestrator_client,
):
    """A record() call should be visible in /ops/health.components."""
    component = f"test.health.{uuid.uuid4().hex[:8]}"
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await service_heartbeats_repo.record(
            db,
            component=component,
            outcome="ok",
            details={"examined": 4},
        )
        await db.commit()

    body = orchestrator_client.get("/ops/health").json()
    components = body["components"]
    matched = [c for c in components if c["component"] == component]
    assert len(matched) == 1
    c = matched[0]
    assert c["last_outcome"] == "ok"
    assert c["details"] == {"examined": 4}
    assert c["consecutive_errors"] == 0
    assert c["seconds_since_last_run"] >= 0
    # Brand-new heartbeat shouldn't be stale (3600s default threshold
    # for components not in the per-component map).
    assert c["is_stale"] is False


async def test_health_endpoint_marks_old_heartbeat_stale(
    orchestrator_client,
):
    """Backdate a heartbeat past the loop's threshold and confirm
    is_stale flips. Uses scheduler.dispatch which has a 180s threshold."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        # Force the timestamp to 30 min ago — well past the 180s
        # threshold for scheduler.dispatch.
        await service_heartbeats_repo.record(
            db,
            component="scheduler.dispatch",
            outcome="ok",
            at=datetime.now(timezone.utc) - timedelta(minutes=30),
        )
        await db.commit()

    body = orchestrator_client.get("/ops/health").json()
    dispatch = next(
        c
        for c in body["components"]
        if c["component"] == "scheduler.dispatch"
    )
    assert dispatch["is_stale"] is True
    assert body["stuck_components"] >= 1


async def test_health_failed_events_counter(orchestrator_client):
    """Insert a failed scheduled_event and confirm the 24h counter
    reflects it. Snapshot the counter before+after so we don't depend
    on the absolute value (other tests seed events too)."""
    before = orchestrator_client.get("/ops/health").json()[
        "failed_events_24h"
    ]

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        evt = ScheduledEvent(
            event_type="health_test_event",
            patient_id=f"health-test-{uuid.uuid4().hex[:6]}",
            scheduled_for=datetime.now(timezone.utc),
            payload={},
            status=ScheduledEventStatus.failed,
            error="health-test fixture",
        )
        db.add(evt)
        await db.flush()
        await db.commit()

    after = orchestrator_client.get("/ops/health").json()[
        "failed_events_24h"
    ]
    assert after >= before + 1


async def test_health_pending_overdue_counter(orchestrator_client):
    """A pending event scheduled for >1h ago counts as 'pending_overdue'."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        evt = ScheduledEvent(
            event_type="health_overdue_test",
            patient_id=f"health-overdue-{uuid.uuid4().hex[:6]}",
            scheduled_for=datetime.now(timezone.utc)
            - timedelta(hours=2),
            payload={},
            status=ScheduledEventStatus.pending,
        )
        db.add(evt)
        await db.flush()
        await db.commit()

    body = orchestrator_client.get("/ops/health").json()
    assert body["pending_overdue"] >= 1

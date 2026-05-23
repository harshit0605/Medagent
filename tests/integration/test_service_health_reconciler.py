"""Integration tests for the service-health reconciler — observability
auto-ticketing.

Covers:
- Stale-component path: a heartbeat older than its threshold opens a
  ``service_health`` ticket; a fresh one does not.
- Errored-component path: ≥3 consecutive errors opens a ticket;
  fewer don't.
- Failed-event burst path: ≥5 failed scheduled_events in 30 min
  opens a ticket.
- Idempotency: repeat passes don't open duplicate tickets while the
  first one is still open. After the ticket is resolved, a new
  recurrence opens a fresh one.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db.models import (
    OpsTicket,
    OpsTicketStatus,
    ScheduledEvent,
    ScheduledEventStatus,
)
from app.db.repositories import ops_tickets as ops_tickets_repo
from app.db.repositories import service_heartbeats as service_heartbeats_repo
from app.db.session import get_sessionmaker
from services.scheduler import service_health_reconciler

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping reconciler tests",
)


async def _open_service_health_tickets_for(
    patient_id: str,
) -> list[OpsTicket]:
    """All currently-open ``service_health`` tickets matching the
    synthetic patient id we wrote. The reconciler keys idempotency
    on this so we use the same key to verify."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        stmt = (
            select(OpsTicket)
            .where(OpsTicket.patient_id == patient_id)
            .where(
                OpsTicket.status.in_(
                    [OpsTicketStatus.open, OpsTicketStatus.acknowledged]
                )
            )
        )
        rows = (await db.execute(stmt)).scalars().all()
        return list(rows)


# ---- Stale heartbeat path -------------------------------------------------


async def test_stale_heartbeat_opens_ticket():
    """Backdated heartbeat past its threshold → reconciler opens a
    ``service_health`` ticket keyed on ``service:<component>:stale``."""
    SessionLocal = get_sessionmaker()
    component = "scheduler.dispatch"  # 180s threshold
    expected_patient = f"service:{component}:stale"

    # Resolve any pre-existing open ticket so this test starts clean.
    async with SessionLocal() as db:
        for t in await _open_service_health_tickets_for(expected_patient):
            await ops_tickets_repo.resolve(db, t.id)
        await service_heartbeats_repo.record(
            db,
            component=component,
            outcome="ok",
            at=datetime.now(timezone.utc) - timedelta(minutes=30),
        )
        await db.commit()

    async with SessionLocal() as db:
        result = await service_health_reconciler.reconcile_service_health(db)
        await db.commit()
    assert result["stale_opened"] >= 1

    open_now = await _open_service_health_tickets_for(expected_patient)
    assert len(open_now) == 1
    assert open_now[0].category == "service_health"
    assert open_now[0].priority == "p1"


async def test_fresh_heartbeat_does_not_open_ticket():
    """A heartbeat written a moment ago is well within threshold → no
    ticket. Critical: the reconciler must not over-page."""
    SessionLocal = get_sessionmaker()
    component = f"test.fresh.{uuid.uuid4().hex[:8]}"  # default 3600s threshold
    expected_patient = f"service:{component}:stale"

    async with SessionLocal() as db:
        await service_heartbeats_repo.record(
            db, component=component, outcome="ok"
        )
        await db.commit()

    async with SessionLocal() as db:
        await service_health_reconciler.reconcile_service_health(db)
        await db.commit()

    open_now = await _open_service_health_tickets_for(expected_patient)
    assert open_now == []


async def test_stale_reconciler_is_idempotent():
    """Running the reconciler twice while a ticket is still open
    must not open a second one."""
    SessionLocal = get_sessionmaker()
    component = "scheduler.recap_sweep"  # 1800s threshold
    expected_patient = f"service:{component}:stale"

    async with SessionLocal() as db:
        for t in await _open_service_health_tickets_for(expected_patient):
            await ops_tickets_repo.resolve(db, t.id)
        await service_heartbeats_repo.record(
            db,
            component=component,
            outcome="ok",
            at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        await db.commit()

    async with SessionLocal() as db:
        first = await service_health_reconciler.reconcile_service_health(db)
        await db.commit()
        second = await service_health_reconciler.reconcile_service_health(db)
        await db.commit()
    assert first["stale_opened"] == 1
    # Second pass sees the open ticket and skips.
    assert second["stale_opened"] == 0
    open_now = await _open_service_health_tickets_for(expected_patient)
    assert len(open_now) == 1

    # After we resolve the ticket, a recurrence reopens.
    async with SessionLocal() as db:
        await ops_tickets_repo.resolve(db, open_now[0].id)
        await db.commit()
        third = await service_health_reconciler.reconcile_service_health(db)
        await db.commit()
    assert third["stale_opened"] == 1


# ---- Errored-component path ----------------------------------------------


async def test_consecutive_errors_open_ticket():
    """3 consecutive errors → ticket opens. Component is still fresh
    (last_run_at = now) so the stale path doesn't fire — only the
    error-run path."""
    SessionLocal = get_sessionmaker()
    component = f"test.errors.{uuid.uuid4().hex[:8]}"
    expected_patient = f"service:{component}:errors"

    async with SessionLocal() as db:
        # 3 consecutive errors → consecutive_errors=3, last_outcome=error
        for _ in range(3):
            await service_heartbeats_repo.record(
                db, component=component, outcome="error"
            )
        await db.commit()

    async with SessionLocal() as db:
        result = await service_health_reconciler.reconcile_service_health(db)
        await db.commit()
    assert result["errored_opened"] >= 1
    open_now = await _open_service_health_tickets_for(expected_patient)
    assert len(open_now) == 1


async def test_one_error_below_threshold_does_not_open():
    """A single error (consecutive_errors=1) is below the threshold of
    3 — no ticket. Avoids paging on transient hiccups."""
    SessionLocal = get_sessionmaker()
    component = f"test.singleerr.{uuid.uuid4().hex[:8]}"
    expected_patient = f"service:{component}:errors"

    async with SessionLocal() as db:
        await service_heartbeats_repo.record(
            db, component=component, outcome="error"
        )
        await db.commit()

    async with SessionLocal() as db:
        await service_health_reconciler.reconcile_service_health(db)
        await db.commit()

    assert await _open_service_health_tickets_for(expected_patient) == []


# ---- Failed-event burst path ---------------------------------------------


async def test_failed_event_burst_opens_ticket():
    """Insert ≥5 failed scheduled_events in the last 30 min → ticket
    opens keyed on ``service:scheduled_events:failed_burst``."""
    expected_patient = "service:scheduled_events:failed_burst"
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        for t in await _open_service_health_tickets_for(expected_patient):
            await ops_tickets_repo.resolve(db, t.id)
        for _ in range(6):
            db.add(
                ScheduledEvent(
                    event_type="recon_test",
                    patient_id=f"recon-burst-{uuid.uuid4().hex[:6]}",
                    scheduled_for=datetime.now(timezone.utc),
                    payload={},
                    status=ScheduledEventStatus.failed,
                    error="reconciler test fixture",
                )
            )
        await db.commit()

    async with SessionLocal() as db:
        result = await service_health_reconciler.reconcile_service_health(db)
        await db.commit()
    assert result["failed_burst_opened"] >= 1
    open_now = await _open_service_health_tickets_for(expected_patient)
    assert len(open_now) == 1

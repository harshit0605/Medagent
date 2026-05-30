"""Integration tests for the per-patient delivery reconciliation sweep.

Verifies the "silent patient" detection: a recipient with persistent failed
deliveries and no successes gets a ``patient_unreachable`` ticket; one who is
reachable again gets it auto-resolved; a patient with mixed failures +
successes is NOT flagged.

Marked serial — the sweep scans whole-table status aggregates, so it would
cross-contaminate with other global-state tests under xdist.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.db.repositories import (
    ops_tickets as ops_tickets_repo,
    whatsapp_statuses as whatsapp_statuses_repo,
)
from app.db.session import get_sessionmaker
from services.scheduler import delivery_reconcile_sweep as sweep

pytestmark = [
    pytest.mark.skipif(
        not os.getenv("DATABASE_URL"),
        reason="DATABASE_URL not set — skipping integration tests",
    ),
    pytest.mark.serial,
]


async def _seed_status(
    *, recipient: str, status: str, when: datetime, error_code: int | None = None
) -> None:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await whatsapp_statuses_repo.upsert(
            db,
            wamid=f"wamid-{uuid.uuid4().hex}",
            status=status,
            recipient_id=recipient,
            timestamp=when,
            error_code=error_code,
            error_title="Message undeliverable" if error_code else None,
        )
        await db.commit()


async def _run_sweep() -> dict:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        out = await sweep.sweep_unreachable_patients(db)
        await db.commit()
    return out


async def test_persistent_failures_open_unreachable_ticket():
    recipient = f"unreach-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    # 3 failed deliveries, zero successes, all within the window.
    for i in range(3):
        await _seed_status(
            recipient=recipient,
            status="failed",
            when=now - timedelta(hours=i + 1),
            error_code=131026,
        )

    out = await _run_sweep()
    assert out["opened"] >= 1

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        ticket = await ops_tickets_repo.find_open_for_patient_category(
            db, patient_id=recipient, category=sweep.CATEGORY
        )
    assert ticket is not None
    assert "unreachable" in (ticket.notes or "").lower()

    # Cleanup
    async with SessionLocal() as db:
        await ops_tickets_repo.resolve(db, ticket.id, actor="test")
        await db.commit()


async def test_idempotent_no_second_ticket():
    recipient = f"unreach-idem-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    for i in range(4):
        await _seed_status(
            recipient=recipient,
            status="failed",
            when=now - timedelta(hours=i + 1),
            error_code=131026,
        )

    await _run_sweep()
    await _run_sweep()  # second pass must not open a duplicate

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        tickets = await ops_tickets_repo.list_for_patient_by_category(
            db, patient_id=recipient, category=sweep.CATEGORY
        )
        open_tickets = [
            t for t in tickets if t.status.value in ("open", "acknowledged")
        ]
    assert len(open_tickets) == 1, "second sweep must not duplicate the ticket"

    async with SessionLocal() as db:
        await ops_tickets_repo.resolve(db, open_tickets[0].id, actor="test")
        await db.commit()


async def test_mixed_failures_and_successes_not_flagged():
    recipient = f"reachable-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    # 3 failures BUT also a delivered — this patient is reachable.
    for i in range(3):
        await _seed_status(
            recipient=recipient,
            status="failed",
            when=now - timedelta(hours=i + 1),
            error_code=131026,
        )
    await _seed_status(
        recipient=recipient, status="delivered", when=now
    )

    await _run_sweep()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        ticket = await ops_tickets_repo.find_open_for_patient_category(
            db, patient_id=recipient, category=sweep.CATEGORY
        )
    assert ticket is None, "a patient with any successful delivery is reachable"


async def test_auto_resolves_when_reachable_again():
    recipient = f"recover-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    for i in range(3):
        await _seed_status(
            recipient=recipient,
            status="failed",
            when=now - timedelta(hours=i + 1),
            error_code=131026,
        )
    await _run_sweep()  # opens the ticket

    # Now a delivered arrives — patient is reachable again.
    await _seed_status(recipient=recipient, status="delivered", when=now)
    out = await _run_sweep()
    assert out["auto_resolved"] >= 1

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        ticket = await ops_tickets_repo.find_open_for_patient_category(
            db, patient_id=recipient, category=sweep.CATEGORY
        )
    assert ticket is None, "ticket should auto-resolve once reachable"

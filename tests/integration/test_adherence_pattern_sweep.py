"""Integration tests for the adherence-drop sweep.

End-to-end against real Postgres because:
    1. The aggregation joins ``adherence_events`` + ``patients``
       and groups by status — only meaningful against a real
       planner (lowering varies by dialect).
    2. Status enum filtering uses the SQLAlchemy IN clause
       against the real Postgres enum type.
    3. The window-bound time filter exercises real timestamp
       comparisons.

Skipped when DATABASE_URL is unset.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import (
    AdherenceEvent,
    AdherenceStatus,
    Patient,
    Regimen,
)
from app.db.repositories import ops_tickets as ops_tickets_repo
from app.db.session import get_sessionmaker
from services.scheduler import adherence_pattern_sweep as sweep

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping adherence sweep tests",
)


async def _seed_patient_with_adherence(
    *,
    taken: int,
    missed: int,
    skipped: int,
    age_days: int = 1,
) -> tuple[int, str]:
    """Create a patient + regimen + ``taken+missed+skipped``
    adherence rows with the given status mix, all within
    ``age_days`` of now (so the default 7-day window picks them
    up)."""
    suffix = uuid.uuid4().hex[:8]
    when = datetime.now(timezone.utc) - timedelta(days=age_days)
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(
            full_name=f"Adherence Test {suffix}",
            phone=f"adherence-{suffix}",
            consent_sms=True,
        )
        db.add(p)
        await db.flush()
        regimen = Regimen(
            patient_id=p.id,
            medication_name="Metformin",
            dose="500 mg",
            schedule={"type": "times_of_day", "times": ["08:00"]},
        )
        db.add(regimen)
        await db.flush()

        # One adherence row per status × count.
        i = 0
        for status, count in (
            (AdherenceStatus.taken, taken),
            (AdherenceStatus.missed, missed),
            (AdherenceStatus.skipped, skipped),
        ):
            for _ in range(count):
                # Stagger by minutes so the unique-constraint on
                # (regimen_id, scheduled_at) doesn't collide.
                event_at = when + timedelta(minutes=i)
                i += 1
                db.add(
                    AdherenceEvent(
                        patient_id=p.id,
                        regimen_id=regimen.id,
                        scheduled_at=event_at,
                        status=status,
                    )
                )
        await db.commit()
        return p.id, p.phone


# ---- Drop opens a ticket -------------------------------------------------


async def test_drop_opens_ticket(monkeypatch):
    """Tighten min volume + threshold via env so a fresh patient
    with a clear drop fires the alert."""
    monkeypatch.setenv("ADHERENCE_DROP_MIN_SCHEDULED", "5")
    pid, phone = await _seed_patient_with_adherence(
        taken=1, missed=4, skipped=0
    )  # 20% rate

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        out = await sweep.sweep_adherence_drops(db)
        await db.commit()

    assert pid in out["opened_patient_ids"]

    # Confirm the ticket exists with the documented contract.
    async with SessionLocal() as db:
        existing = (
            await ops_tickets_repo.find_open_for_patient_category(
                db, patient_id=phone, category="adherence_drop"
            )
        )
        assert existing is not None
        assert existing.priority == "high"
        assert existing.sla_minutes == 1440
        assert "20.0%" in (existing.notes or "")


async def test_volume_below_floor_no_ticket(monkeypatch):
    """Even with 100% missed (worst possible rate), a 3-event
    window is below the default floor — no ticket. A patient with
    sparse data is too thin a signal to alarm."""
    monkeypatch.setenv("ADHERENCE_DROP_MIN_SCHEDULED", "7")
    pid, phone = await _seed_patient_with_adherence(
        taken=0, missed=3, skipped=0
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        out = await sweep.sweep_adherence_drops(db)
        await db.commit()
        existing = (
            await ops_tickets_repo.find_open_for_patient_category(
                db, patient_id=phone, category="adherence_drop"
            )
        )

    assert existing is None
    assert pid not in out.get("opened_patient_ids", [])


# ---- Idempotency + auto-resolve -------------------------------------------


async def test_re_sweep_is_idempotent(monkeypatch):
    """Two consecutive sweeps over the same dropping patient must
    produce ONE ticket, not two."""
    monkeypatch.setenv("ADHERENCE_DROP_MIN_SCHEDULED", "5")
    pid, phone = await _seed_patient_with_adherence(
        taken=1, missed=4, skipped=0
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await sweep.sweep_adherence_drops(db)
        await db.commit()
    async with SessionLocal() as db:
        await sweep.sweep_adherence_drops(db)
        await db.commit()

    async with SessionLocal() as db:
        all_tickets = await ops_tickets_repo.list_for_patient_by_category(
            db, phone, "adherence_drop"
        )
    # Single open ticket regardless of how many sweeps fired.
    open_tickets = [t for t in all_tickets if t.status.value != "resolved"]
    assert len(open_tickets) == 1


async def test_recovery_auto_resolves(monkeypatch):
    """Patient drops → ticket opens. Then their adherence
    recovers (80%), next sweep auto-resolves the existing
    ticket. Without this, a patient who got back on track would
    have a stale ticket cluttering the doctor's digest."""
    monkeypatch.setenv("ADHERENCE_DROP_MIN_SCHEDULED", "5")
    pid, phone = await _seed_patient_with_adherence(
        taken=1, missed=4, skipped=0
    )

    SessionLocal = get_sessionmaker()
    # First sweep → opens ticket.
    async with SessionLocal() as db:
        await sweep.sweep_adherence_drops(db)
        await db.commit()

    # Add 4 more taken events so the rate climbs to 5/9 (~55%) +
    # 5 more taken so it crosses recovery → 9/13 ≈ 69%, still
    # below 75%. Add even more to clearly cross. Aim for 13/17
    # = 76.5% > 75% recovery threshold.
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        regimen = (
            await db.execute(
                __import__("sqlalchemy").select(Regimen).where(
                    Regimen.patient_id == pid
                )
            )
        ).scalars().first()
        when = datetime.now(timezone.utc)
        for j in range(13):
            db.add(
                AdherenceEvent(
                    patient_id=pid,
                    regimen_id=regimen.id,
                    scheduled_at=when - timedelta(minutes=100 + j),
                    status=AdherenceStatus.taken,
                )
            )
        await db.commit()

    # Second sweep → should auto-resolve.
    async with SessionLocal() as db:
        out = await sweep.sweep_adherence_drops(db)
        await db.commit()

    assert pid in out["auto_resolved_patient_ids"]
    async with SessionLocal() as db:
        existing = (
            await ops_tickets_repo.find_open_for_patient_category(
                db, patient_id=phone, category="adherence_drop"
            )
        )
        # No more open ticket — got resolved.
        assert existing is None


# ---- Window enforcement --------------------------------------------------


async def test_old_events_outside_window_excluded(monkeypatch):
    """Events older than the rolling window must NOT count
    toward the rate. Otherwise an old patient with stale data
    would trigger alerts forever."""
    monkeypatch.setenv("ADHERENCE_DROP_MIN_SCHEDULED", "5")
    monkeypatch.setenv("ADHERENCE_DROP_WINDOW_DAYS", "7")
    # Seed events from 30 days ago — well outside the 7-day window.
    pid, phone = await _seed_patient_with_adherence(
        taken=1, missed=4, skipped=0, age_days=30
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        out = await sweep.sweep_adherence_drops(db)
        await db.commit()
        existing = (
            await ops_tickets_repo.find_open_for_patient_category(
                db, patient_id=phone, category="adherence_drop"
            )
        )

    # Patient should not even be EVALUATED (no in-window rows).
    assert pid not in out.get("opened_patient_ids", [])
    assert existing is None

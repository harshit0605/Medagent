"""DB-level idempotency for one-shot materialized reminders.

The pregnancy/post-op materializers now enqueue via ``enqueue_idempotent``,
backed by a partial unique index on ``scheduled_events.idempotency_key``. This
verifies the DB backstop: a repeat (or concurrent) enqueue of the same key
inserts at most once, and a full materialize pass run twice produces no
duplicate events.

DATABASE_URL required.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db.models import Patient, ScheduledEvent
from app.db.repositories import pregnancies as pregnancies_repo
from app.db.repositories import scheduled_events as se_repo
from app.db.session import get_sessionmaker
from services.scheduler import pregnancy_milestones as pm

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping idempotency tests",
)


async def test_enqueue_idempotent_dedupes_on_key():
    SessionLocal = get_sessionmaker()
    key = f"test-idem:{uuid.uuid4().hex}"
    phone = f"idem-{uuid.uuid4().hex[:8]}"

    async with SessionLocal() as db:
        first = await se_repo.enqueue_idempotent(
            db,
            event_type="pregnancy_weekly_due",
            patient_id=phone,
            payload={"x": 1},
            idempotency_key=key,
            scheduled_for=datetime.now(timezone.utc),
        )
        await db.commit()
    assert first is not None

    # Second enqueue with the SAME key — conflict, returns None, no new row.
    async with SessionLocal() as db:
        second = await se_repo.enqueue_idempotent(
            db,
            event_type="pregnancy_weekly_due",
            patient_id=phone,
            payload={"x": 2},
            idempotency_key=key,
            scheduled_for=datetime.now(timezone.utc),
        )
        await db.commit()
    assert second is None

    async with SessionLocal() as db:
        rows = list(
            (
                await db.execute(
                    select(ScheduledEvent).where(
                        ScheduledEvent.idempotency_key == key
                    )
                )
            ).scalars().all()
        )
    assert len(rows) == 1
    assert rows[0].payload == {"x": 1}  # the first writer won


async def test_materialize_pregnancy_twice_is_idempotent():
    suffix = uuid.uuid4().hex[:8]
    phone = f"idem-preg-{suffix}"
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(full_name=f"Idem Preg {suffix}", phone=phone, consent_sms=True)
        db.add(p)
        await db.flush()
        # LMP ~10 weeks ago → several future milestones + weekly check-ins.
        lmp = datetime.now(timezone.utc).date() - timedelta(weeks=10)
        await pregnancies_repo.create(db, patient_id=p.id, lmp_date=lmp)
        await db.commit()
        pid = p.id

    async def _materialize_once():
        async with SessionLocal() as db:
            preg = await pregnancies_repo.get_active_for_patient(db, pid)
            await pm.materialize_for_pregnancy(db, preg, patient_phone=phone)
            await db.commit()

    await _materialize_once()
    await _materialize_once()  # second pass must not duplicate

    async with SessionLocal() as db:
        rows = list(
            (
                await db.execute(
                    select(ScheduledEvent)
                    .where(ScheduledEvent.patient_id == phone)
                    .where(ScheduledEvent.event_type.in_(pm._OUR_EVENT_TYPES))
                )
            ).scalars().all()
        )
    assert rows, "expected materialized events"
    keys = [r.idempotency_key for r in rows]
    # No duplicate idempotency keys → no duplicate reminders.
    assert len(keys) == len(set(keys))
    assert all(k is not None for k in keys)

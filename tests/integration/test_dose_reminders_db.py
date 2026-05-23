"""Integration tests for dose reminder materialize / cancel against the DB.

Covers idempotency (re-running materialize is a no-op) and the cancel path
(both ScheduledEvent and AdherenceEvent flip to skipped).
"""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.db.models import (
    AdherenceEvent,
    AdherenceStatus,
    Patient,
    Regimen,
    ScheduledEvent,
    ScheduledEventStatus,
)
from app.db.session import get_sessionmaker
from services.scheduler import dose_reminders

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping dose reminder integration tests",
)


async def _make_patient_and_regimen(
    db,
    *,
    times: list[str] | None = None,
    starts_on: date | None = None,
    ends_on: date | None = None,
) -> tuple[Patient, Regimen]:
    suffix = uuid.uuid4().hex[:8]
    patient = Patient(
        full_name=f"Dose Test {suffix}",
        phone=f"dose-test-{suffix}",
    )
    db.add(patient)
    await db.flush()
    await db.refresh(patient)

    regimen = Regimen(
        patient_id=patient.id,
        medication_name="Metformin",
        dose="500 mg",
        schedule={
            "type": "times_of_day",
            "times": times or ["08:00", "20:00"],
            "timezone": "UTC",
        },
        starts_on=starts_on,
        ends_on=ends_on,
    )
    db.add(regimen)
    await db.flush()
    await db.refresh(regimen)
    return patient, regimen


async def _cleanup(db, *, patient_id: int, regimen_id: int, patient_phone: str):
    await db.execute(
        delete(ScheduledEvent).where(ScheduledEvent.patient_id == patient_phone)
    )
    await db.execute(
        delete(AdherenceEvent).where(AdherenceEvent.regimen_id == regimen_id)
    )
    await db.execute(delete(Regimen).where(Regimen.id == regimen_id))
    await db.execute(delete(Patient).where(Patient.id == patient_id))
    await db.commit()


async def test_materialize_creates_paired_adherence_and_scheduled_rows():
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        patient, regimen = await _make_patient_and_regimen(db)
        try:
            # 48h window starting at the next midnight UTC so we predictably
            # hit 4 occurrences (08:00 + 20:00 across two days).
            start = datetime.combine(
                datetime.now(timezone.utc).date() + timedelta(days=1),
                datetime.min.time(),
                tzinfo=timezone.utc,
            )
            end = start + timedelta(days=2)
            created = await dose_reminders.materialize_for_regimen(
                db,
                regimen,
                patient_phone=patient.phone,
                window_start=start,
                window_end=end,
            )
            await db.commit()
            assert len(created) == 4

            # Verify each scheduled event has a matching adherence row.
            scheduled_rows = list(
                (
                    await db.execute(
                        select(ScheduledEvent).where(
                            ScheduledEvent.patient_id == patient.phone
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(scheduled_rows) == 4
            adherence_ids = {
                r.payload["adherence_event_id"] for r in scheduled_rows
            }
            adherence_rows = list(
                (
                    await db.execute(
                        select(AdherenceEvent).where(
                            AdherenceEvent.regimen_id == regimen.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert {a.id for a in adherence_rows} == adherence_ids
            assert all(
                a.status == AdherenceStatus.scheduled for a in adherence_rows
            )
        finally:
            await _cleanup(
                db,
                patient_id=patient.id,
                regimen_id=regimen.id,
                patient_phone=patient.phone,
            )


async def test_materialize_is_idempotent_on_repeated_runs():
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        patient, regimen = await _make_patient_and_regimen(db)
        try:
            start = datetime.combine(
                datetime.now(timezone.utc).date() + timedelta(days=1),
                datetime.min.time(),
                tzinfo=timezone.utc,
            )
            end = start + timedelta(days=2)
            await dose_reminders.materialize_for_regimen(
                db, regimen, patient_phone=patient.phone,
                window_start=start, window_end=end,
            )
            await db.commit()

            # Second run with the same window should create zero new events.
            second = await dose_reminders.materialize_for_regimen(
                db, regimen, patient_phone=patient.phone,
                window_start=start, window_end=end,
            )
            await db.commit()
            assert second == []
        finally:
            await _cleanup(
                db,
                patient_id=patient.id,
                regimen_id=regimen.id,
                patient_phone=patient.phone,
            )


async def test_cancel_for_regimen_skips_pending_dose_events():
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        patient, regimen = await _make_patient_and_regimen(db)
        try:
            start = datetime.combine(
                datetime.now(timezone.utc).date() + timedelta(days=1),
                datetime.min.time(),
                tzinfo=timezone.utc,
            )
            end = start + timedelta(days=2)
            await dose_reminders.materialize_for_regimen(
                db, regimen, patient_phone=patient.phone,
                window_start=start, window_end=end,
            )
            await db.commit()

            cancelled = await dose_reminders.cancel_for_regimen(
                db, regimen_id=regimen.id, reason="test_cancel"
            )
            await db.commit()
            assert cancelled == 4

            scheduled_rows = list(
                (
                    await db.execute(
                        select(ScheduledEvent).where(
                            ScheduledEvent.patient_id == patient.phone
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert all(
                r.status == ScheduledEventStatus.skipped for r in scheduled_rows
            )
            adherence_rows = list(
                (
                    await db.execute(
                        select(AdherenceEvent).where(
                            AdherenceEvent.regimen_id == regimen.id
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert all(
                a.status == AdherenceStatus.skipped for a in adherence_rows
            )
        finally:
            await _cleanup(
                db,
                patient_id=patient.id,
                regimen_id=regimen.id,
                patient_phone=patient.phone,
            )

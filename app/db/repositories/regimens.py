"""Persistence for medication regimens.

A regimen describes WHAT a patient takes and WHEN — medication name + dose +
schedule. The schedule is JSON; the materializer
(``services/scheduler/dose_reminders.py``) converts it into per-occurrence
``AdherenceEvent`` rows + ``ScheduledEvent`` queue entries.

Schedule format (one supported variant for now — extend later):
    {
        "type": "times_of_day",
        "times": ["08:00", "20:00"],
        "timezone": "Asia/Kolkata",
        "frequency": "daily"
    }
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import asc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Regimen


async def create(
    session: AsyncSession,
    *,
    patient_id: int,
    medication_name: str,
    dose: str,
    schedule: dict[str, Any],
    starts_on: date | None = None,
    ends_on: date | None = None,
    strict_timing: bool = False,
    prescription_id: int | None = None,
    supply_days_initial: int | None = None,
    supply_started_on: date | None = None,
) -> Regimen:
    row = Regimen(
        patient_id=patient_id,
        medication_name=medication_name,
        dose=dose,
        schedule=schedule,
        starts_on=starts_on,
        ends_on=ends_on,
        strict_timing=strict_timing,
        prescription_id=prescription_id,
        supply_days_initial=supply_days_initial,
        supply_started_on=supply_started_on,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def reset_supply(
    session: AsyncSession,
    regimen_id: int,
    *,
    on: date,
    supply_days_initial: int | None = None,
) -> Regimen | None:
    """Mark a fresh refill cycle. ``on`` is the day the patient picked up
    the new supply; the materializer treats refill reminders for the next
    cycle relative to ``(on + supply_days_initial)``. When
    ``supply_days_initial`` is omitted, the existing value is preserved."""
    row = await session.get(Regimen, regimen_id)
    if row is None:
        return None
    row.supply_started_on = on
    if supply_days_initial is not None:
        row.supply_days_initial = supply_days_initial
    await session.flush()
    await session.refresh(row)
    return row


async def get(session: AsyncSession, regimen_id: int) -> Regimen | None:
    return await session.get(Regimen, regimen_id)


async def list_for_patient(
    session: AsyncSession,
    patient_id: int,
    *,
    active_on: date | None = None,
) -> list[Regimen]:
    """All regimens for a patient. When ``active_on`` is given, restrict to
    regimens that are active on that date (starts_on ≤ date ≤ ends_on, where
    None on either end means open-ended)."""
    stmt = (
        select(Regimen)
        .where(Regimen.patient_id == patient_id)
        .order_by(asc(Regimen.id))
    )
    rows = list((await session.execute(stmt)).scalars().all())
    if active_on is None:
        return rows
    return [
        r
        for r in rows
        if (r.starts_on is None or r.starts_on <= active_on)
        and (r.ends_on is None or r.ends_on >= active_on)
    ]


async def list_active_today(session: AsyncSession, today: date) -> list[Regimen]:
    """All regimens active on ``today`` across every patient — used by the
    scheduler's dose-materialization loop."""
    stmt = select(Regimen).order_by(asc(Regimen.id))
    rows = list((await session.execute(stmt)).scalars().all())
    return [
        r
        for r in rows
        if (r.starts_on is None or r.starts_on <= today)
        and (r.ends_on is None or r.ends_on >= today)
    ]


async def deactivate(
    session: AsyncSession, regimen_id: int, *, on: date
) -> Regimen | None:
    """Stop the regimen as of ``on`` by setting ends_on. Pending dose
    reminders for this regimen should be cancelled separately via
    :func:`services.scheduler.dose_reminders.cancel_for_regimen`."""
    row = await session.get(Regimen, regimen_id)
    if row is None:
        return None
    row.ends_on = on
    await session.flush()
    await session.refresh(row)
    return row

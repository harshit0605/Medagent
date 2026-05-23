"""Household persistence (multi-patient households).

A household groups several patients so one caregiver oversees 1→many. Members
are linked via ``Patient.household_id``.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Household, Patient


async def get(session: AsyncSession, household_id: int) -> Household | None:
    return await session.get(Household, household_id)


async def create(
    session: AsyncSession,
    *,
    name: str,
    primary_caregiver_phone: str | None = None,
    notes: str | None = None,
) -> Household:
    row = Household(
        name=name[:255],
        primary_caregiver_phone=primary_caregiver_phone,
        notes=notes,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def list_members(
    session: AsyncSession, household_id: int
) -> list[Patient]:
    stmt = (
        select(Patient)
        .where(Patient.household_id == household_id)
        .where(Patient.erased_at.is_(None))
        .order_by(Patient.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def add_member(
    session: AsyncSession, *, household_id: int, patient_id: int
) -> Patient | None:
    """Link a patient to a household. Returns the updated patient, or ``None``
    if the patient or household doesn't exist."""
    household = await session.get(Household, household_id)
    patient = await session.get(Patient, patient_id)
    if household is None or patient is None:
        return None
    patient.household_id = household_id
    await session.flush()
    await session.refresh(patient)
    return patient


async def remove_member(
    session: AsyncSession, *, patient_id: int
) -> Patient | None:
    patient = await session.get(Patient, patient_id)
    if patient is None:
        return None
    patient.household_id = None
    await session.flush()
    await session.refresh(patient)
    return patient

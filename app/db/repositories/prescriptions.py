"""Persistence for patient-uploaded prescription images.

Workflow::

    pending  → vision LLM parses → still pending (clinician must review)
    pending  → clinician edits parsed_payload → verifies → creates Regimens
    pending  → clinician rejects → terminal

The orchestrator's ``prescription_handler`` creates rows on inbound and
fills ``parsed_payload`` with a ``ParsedPrescription`` dict the ops
console renders for review.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Prescription, VerificationStatus


async def create(
    session: AsyncSession,
    *,
    patient_id: int,
    source_upload_url: str,
    parsed_payload: dict[str, Any] | None = None,
) -> Prescription:
    row = Prescription(
        patient_id=patient_id,
        source_upload_url=source_upload_url,
        parsed_payload=parsed_payload or {},
        human_verification_status=VerificationStatus.pending,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def get(
    session: AsyncSession, prescription_id: int
) -> Prescription | None:
    return await session.get(Prescription, prescription_id)


async def list_pending(
    session: AsyncSession, *, limit: int = 100
) -> list[Prescription]:
    """Used by the ops console queue page — newest first."""
    stmt = (
        select(Prescription)
        .where(
            Prescription.human_verification_status == VerificationStatus.pending
        )
        .order_by(desc(Prescription.created_at))
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_for_patient(
    session: AsyncSession, patient_id: int, *, limit: int = 50
) -> list[Prescription]:
    stmt = (
        select(Prescription)
        .where(Prescription.patient_id == patient_id)
        .order_by(desc(Prescription.created_at))
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def set_parsed_payload(
    session: AsyncSession,
    prescription_id: int,
    *,
    parsed_payload: dict[str, Any],
) -> Prescription | None:
    """Overwrite ``parsed_payload`` (e.g. clinician edits before verify,
    or the vision parser landing its result)."""
    row = await session.get(Prescription, prescription_id)
    if row is None:
        return None
    row.parsed_payload = parsed_payload
    await session.flush()
    await session.refresh(row)
    return row


async def mark_verified(
    session: AsyncSession,
    prescription_id: int,
    *,
    verified_by: str,
    at: datetime | None = None,
) -> Prescription | None:
    row = await session.get(Prescription, prescription_id)
    if row is None:
        return None
    row.human_verification_status = VerificationStatus.verified
    row.verified_at = (at or datetime.now(timezone.utc))
    if row.verified_at.tzinfo is None:
        row.verified_at = row.verified_at.replace(tzinfo=timezone.utc)
    row.verified_by = verified_by
    await session.flush()
    await session.refresh(row)
    return row


async def mark_rejected(
    session: AsyncSession,
    prescription_id: int,
    *,
    rejected_by: str,
    at: datetime | None = None,
) -> Prescription | None:
    row = await session.get(Prescription, prescription_id)
    if row is None:
        return None
    row.human_verification_status = VerificationStatus.rejected
    # We don't have a separate rejected_at column — reuse verified_at as
    # a "decision time" timestamp; verified_by carries the decider.
    row.verified_at = (at or datetime.now(timezone.utc))
    if row.verified_at.tzinfo is None:
        row.verified_at = row.verified_at.replace(tzinfo=timezone.utc)
    row.verified_by = rejected_by
    await session.flush()
    await session.refresh(row)
    return row

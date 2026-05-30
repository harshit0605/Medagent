"""Dual-control erasure-request lifecycle.

The two-person rule for patient erasure: one operator files a request, a
DIFFERENT operator approves it (which then executes the scrub). This repo is
the state machine; the actual PHI scrub stays in ``patient_erasure``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ErasureRequest

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_CANCELLED = "cancelled"


class SelfApprovalError(Exception):
    """Raised when the approver is the same operator who filed the request."""


async def get(session: AsyncSession, request_id: int) -> ErasureRequest | None:
    return await session.get(ErasureRequest, request_id)


async def get_pending_for_patient(
    session: AsyncSession, patient_id: int
) -> ErasureRequest | None:
    stmt = (
        select(ErasureRequest)
        .where(ErasureRequest.patient_id == patient_id)
        .where(ErasureRequest.status == STATUS_PENDING)
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def list_pending(session: AsyncSession) -> list[ErasureRequest]:
    """All open erasure requests awaiting a second operator's approval —
    powers the ops-console approval queue."""
    stmt = (
        select(ErasureRequest)
        .where(ErasureRequest.status == STATUS_PENDING)
        .order_by(desc(ErasureRequest.requested_at))
    )
    return list((await session.execute(stmt)).scalars().all())


async def create(
    session: AsyncSession,
    *,
    patient_id: int,
    requested_by: str,
    reason: str,
) -> ErasureRequest:
    """File a new erasure request. Caller should first check
    ``get_pending_for_patient`` — the partial unique index also enforces
    one-pending-per-patient at the DB level."""
    row = ErasureRequest(
        patient_id=patient_id,
        requested_by=requested_by[:128],
        reason=reason[:255],
        status=STATUS_PENDING,
    )
    session.add(row)
    await session.flush()
    return row


async def approve(
    session: AsyncSession,
    request_id: int,
    *,
    approved_by: str,
    at: datetime | None = None,
) -> ErasureRequest | None:
    """Approve a pending request. Raises :class:`SelfApprovalError` if the
    approver is the same operator who filed it (the whole point of the rule).
    Returns the updated row (status=approved), or None if the request doesn't
    exist / isn't pending. The caller runs the scrub on a successful approve."""
    row = await session.get(ErasureRequest, request_id)
    if row is None or row.status != STATUS_PENDING:
        return None
    if row.requested_by == approved_by:
        raise SelfApprovalError(
            "the approver must be a different operator than the requester"
        )
    row.status = STATUS_APPROVED
    row.approved_by = approved_by[:128]
    row.resolved_at = at or datetime.now(timezone.utc)
    await session.flush()
    return row


async def reject(
    session: AsyncSession,
    request_id: int,
    *,
    rejected_by: str,
    at: datetime | None = None,
) -> ErasureRequest | None:
    row = await session.get(ErasureRequest, request_id)
    if row is None or row.status != STATUS_PENDING:
        return None
    row.status = STATUS_REJECTED
    row.approved_by = rejected_by[:128]
    row.resolved_at = at or datetime.now(timezone.utc)
    await session.flush()
    return row

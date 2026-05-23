"""Clinical alert persistence.

The triage flow creates rows; the ops console acks + resolves
them. We don't expose update helpers for fields like severity
or summary — alerts are evidence rows; mutating them would
break audit. Re-classification creates a new row.

Status transitions:

    open → acknowledged → resolved

``acknowledged`` is the doctor saying "I see it"; ``resolved``
is "I've handled it (called the patient / dispatched care /
confirmed false positive)" plus an optional resolution note
that's read during chart review.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ClinicalAlert


# Severity ordering used by the open-queue sort. Critical
# floats to the top; anything outside the known set sorts last.
_SEVERITY_RANK: dict[str, int] = {"critical": 0, "high": 1}


async def create(
    session: AsyncSession,
    *,
    patient_id: int,
    patient_phone: str | None,
    message_id: int | None,
    severity: str,
    red_flags: list[str],
    clinical_summary: str,
    inbound_text: str,
    llm_model: str | None,
) -> ClinicalAlert:
    if severity not in _SEVERITY_RANK:
        raise ValueError(
            f"unsupported severity {severity!r}; "
            f"valid: {sorted(_SEVERITY_RANK)}"
        )
    row = ClinicalAlert(
        patient_id=patient_id,
        patient_phone=patient_phone,
        message_id=message_id,
        severity=severity,
        red_flags=red_flags,
        clinical_summary=clinical_summary,
        inbound_text=inbound_text,
        llm_model=llm_model,
        status="open",
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def get(
    session: AsyncSession, alert_id: int
) -> ClinicalAlert | None:
    return await session.get(ClinicalAlert, alert_id)


async def list_recent(
    session: AsyncSession,
    *,
    status: str | None = None,
    limit: int = 100,
) -> list[ClinicalAlert]:
    """List alerts newest-first within a status filter. The
    open-queue view passes ``status="open"``; the audit view
    omits the filter to see everything chronologically."""
    stmt = (
        select(ClinicalAlert)
        .order_by(desc(ClinicalAlert.created_at))
        .limit(limit)
    )
    if status is not None:
        stmt = stmt.where(ClinicalAlert.status == status)
    return list((await session.execute(stmt)).scalars().all())


async def list_for_patient(
    session: AsyncSession,
    patient_id: int,
    *,
    limit: int = 20,
) -> list[ClinicalAlert]:
    stmt = (
        select(ClinicalAlert)
        .where(ClinicalAlert.patient_id == patient_id)
        .order_by(desc(ClinicalAlert.created_at))
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def acknowledge(
    session: AsyncSession,
    alert_id: int,
    *,
    actor: str,
) -> ClinicalAlert | None:
    """Flip ``open → acknowledged``. No-op if the alert is
    already past open (idempotent for double-clicks)."""
    row = await session.get(ClinicalAlert, alert_id)
    if row is None or row.status != "open":
        return row
    row.status = "acknowledged"
    row.acknowledged_at = datetime.now(timezone.utc)
    row.acknowledged_by = actor
    await session.flush()
    return row


async def resolve(
    session: AsyncSession,
    alert_id: int,
    *,
    actor: str,
    notes: str | None,
) -> ClinicalAlert | None:
    """Flip to ``resolved``. Allowed from ``open`` or
    ``acknowledged`` (a doctor may resolve directly without an
    intermediate ack click). Idempotent — double-resolve is
    rejected as None."""
    row = await session.get(ClinicalAlert, alert_id)
    if row is None or row.status == "resolved":
        return row
    now = datetime.now(timezone.utc)
    if row.status == "open":
        row.acknowledged_at = now
        row.acknowledged_by = actor
    row.status = "resolved"
    row.resolved_at = now
    row.resolved_by = actor
    if notes:
        row.resolution_notes = notes[:2000]
    await session.flush()
    return row


async def mark_paged(
    session: AsyncSession,
    alert_id: int,
    *,
    doctor_id: int | None,
) -> ClinicalAlert | None:
    """Stamp paging audit columns and bump attempt counter.

    ``doctor_id`` is None when the pager couldn't resolve a
    target — we still bump ``paged_attempts`` and stamp
    ``paged_at`` so the re-page sweep can see "we tried at
    14:00 and there was nobody to page" rather than thinking
    no attempt was made (which would loop forever).
    """
    row = await session.get(ClinicalAlert, alert_id)
    if row is None:
        return None
    row.paged_doctor_id = doctor_id
    row.paged_at = datetime.now(timezone.utc)
    row.paged_attempts = (row.paged_attempts or 0) + 1
    await session.flush()
    return row


async def list_unacked_for_repage(
    session: AsyncSession,
    *,
    older_than_seconds: int,
    max_attempts: int,
    limit: int = 50,
) -> list[ClinicalAlert]:
    """Re-page sweep query. Returns critical alerts that:

    - are still ``open`` (acknowledged ones don't need re-page),
    - are older than ``older_than_seconds`` since
      ``paged_at`` (or ``created_at`` if never paged),
    - have made ``< max_attempts`` paging attempts.

    Severity filter is hardcoded to ``critical`` here — high
    alerts go in the queue but don't actively page, by design.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=older_than_seconds
    )
    # Use COALESCE(paged_at, created_at) so first-attempt
    # candidates (paged_at IS NULL) also surface once they're
    # past the threshold from creation.
    from sqlalchemy import func as sa_func

    last_attempt = sa_func.coalesce(
        ClinicalAlert.paged_at, ClinicalAlert.created_at
    )
    stmt = (
        select(ClinicalAlert)
        .where(ClinicalAlert.severity == "critical")
        .where(ClinicalAlert.status == "open")
        .where(ClinicalAlert.paged_attempts < max_attempts)
        .where(last_attempt <= cutoff)
        .order_by(ClinicalAlert.created_at.asc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def count_by_status(
    session: AsyncSession,
) -> dict[str, int]:
    """Tile counters for the alerts dashboard. Returns a dict
    keyed by status with counts; missing statuses default to 0
    in caller code, not here."""
    from sqlalchemy import func

    stmt = select(
        ClinicalAlert.status, func.count(ClinicalAlert.id)
    ).group_by(ClinicalAlert.status)
    rows = (await session.execute(stmt)).all()
    return {status: count for status, count in rows}

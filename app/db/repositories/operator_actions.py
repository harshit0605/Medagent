"""Operator action audit log writer + reader.

Append-only — the only mutations are inserts. Surface readers (``list_for_*``)
power the ops-console "who did what" view.

The writer is best-effort by convention: callers wrap it in their own
try/except and never let a bad audit write block a successful action. The
audit row is a record of intent, not a precondition for the action.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OperatorAction


# Stable action codes — extend as needed. Callers should use the constants
# (not raw strings) so ``grep`` finds every audited action site.
ACTION_PATIENT_EXPORT = "patient_export"
ACTION_PATIENT_ERASURE = "patient_erasure"
ACTION_PATIENT_PAUSE = "patient_pause"
ACTION_PATIENT_UNPAUSE = "patient_unpause"
ACTION_TICKET_ACK = "ticket_ack"
ACTION_TICKET_RESOLVE = "ticket_resolve"
ACTION_TICKET_SNOOZE = "ticket_snooze"
ACTION_TICKET_REOPEN = "ticket_reopen"
ACTION_EXEMPTION_GRANT = "exemption_grant"
ACTION_EXEMPTION_REVOKE = "exemption_revoke"


async def record(
    session: AsyncSession,
    *,
    operator_id: str,
    action: str,
    target_type: str,
    target_id: str | int,
    details: dict[str, Any] | None = None,
    at: datetime | None = None,
) -> OperatorAction:
    """Append a single operator action row. Caller commits."""
    row = OperatorAction(
        operator_id=operator_id[:128],
        action=action[:64],
        target_type=target_type[:32],
        target_id=str(target_id)[:128],
        details=details or {},
        logged_at=at or datetime.now(timezone.utc),
    )
    session.add(row)
    await session.flush()
    return row


async def count_recent_actions(
    session: AsyncSession,
    *,
    operator_id: str,
    action: str,
    since,
) -> int:
    """How many ``action`` rows this operator has logged since ``since``.

    Powers the DSAR-export rate limit: a single operator scraping the patient
    DB via repeated exports (e.g. with a leaked API key + signing key) trips a
    per-operator/day ceiling. Counts against the durable audit log, so it
    survives restarts and is consistent across replicas (unlike an in-memory
    rate limiter)."""
    from sqlalchemy import func

    stmt = (
        select(func.count())
        .select_from(OperatorAction)
        .where(OperatorAction.operator_id == operator_id)
        .where(OperatorAction.action == action)
        .where(OperatorAction.logged_at >= since)
    )
    return (await session.execute(stmt)).scalar_one() or 0


async def list_for_operator(
    session: AsyncSession,
    operator_id: str,
    *,
    limit: int = 100,
) -> list[OperatorAction]:
    """Recent actions by a single operator (most recent first). Powers the
    ops-console per-operator audit view."""
    stmt = (
        select(OperatorAction)
        .where(OperatorAction.operator_id == operator_id)
        .order_by(desc(OperatorAction.logged_at))
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_for_target(
    session: AsyncSession,
    *,
    target_type: str,
    target_id: str | int,
    limit: int = 100,
) -> list[OperatorAction]:
    """Recent actions taken against a single target (e.g. one patient).
    Powers the per-patient audit timeline."""
    stmt = (
        select(OperatorAction)
        .where(OperatorAction.target_type == target_type)
        .where(OperatorAction.target_id == str(target_id))
        .order_by(desc(OperatorAction.logged_at))
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())

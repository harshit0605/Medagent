"""Inbound-message dedupe ledger repo.

Backs the orchestrator ``/route`` replay guard. ``claim`` atomically records
that a provider message id (WhatsApp ``wamid``) has been seen: the first
caller wins and processes the message, every later caller (a Meta webhook
redelivery) is told it was already handled and short-circuits.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ProcessedInboundMessage


async def claim(
    session: AsyncSession,
    *,
    message_id: str,
    patient_id: str | None = None,
) -> bool:
    """Atomically claim an inbound message id for processing.

    Returns ``True`` when this call inserted the row (first time we've seen the
    id — the caller should process the message) and ``False`` when the id
    already existed (a replay — the caller should short-circuit). Uses
    ``INSERT ... ON CONFLICT DO NOTHING`` against the ``message_id`` primary
    key, so concurrent redeliveries resolve to exactly one winner (a
    concurrent second insert blocks on the first's uncommitted row and then
    sees the conflict once it commits).

    The INSERT is left uncommitted — the caller's request transaction commits
    it on success and rolls it back on error, so a failed ``/route`` doesn't
    leave a claim behind that would suppress a legitimate retry.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    stmt = (
        pg_insert(ProcessedInboundMessage)
        .values(message_id=message_id, patient_id=patient_id)
        .on_conflict_do_nothing(index_elements=["message_id"])
        .returning(ProcessedInboundMessage.message_id)
    )
    claimed = (await session.execute(stmt)).scalar_one_or_none()
    return claimed is not None

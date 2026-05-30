from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import WhatsAppMessageStatus


# Meta delivers statuses in roughly chronological order, but a `read` can
# occasionally arrive before the matching `delivered` in the same batch. We
# rank statuses so a higher-rank value never gets overwritten by a lower one.
_STATUS_RANK: dict[str, int] = {
    "sent": 1,
    "delivered": 2,
    "read": 3,
    "failed": 4,  # terminal — supersedes everything else
}


def _rank_expr(column):
    return case(
        *((column == status, rank) for status, rank in _STATUS_RANK.items()),
        else_=0,
    )


async def upsert(
    session: AsyncSession,
    *,
    wamid: str,
    status: str,
    recipient_id: str | None,
    timestamp: datetime,
    error_code: int | None = None,
    error_title: str | None = None,
    raw: dict[str, Any] | None = None,
) -> WhatsAppMessageStatus:
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)

    stmt = insert(WhatsAppMessageStatus).values(
        wamid=wamid,
        status=status,
        recipient_id=recipient_id,
        timestamp=timestamp,
        error_code=error_code,
        error_title=error_title,
        raw=raw or {},
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[WhatsAppMessageStatus.wamid],
        set_={
            "status": stmt.excluded.status,
            "recipient_id": stmt.excluded.recipient_id,
            "timestamp": stmt.excluded.timestamp,
            "error_code": stmt.excluded.error_code,
            "error_title": stmt.excluded.error_title,
            "raw": stmt.excluded.raw,
        },
        # Only advance: never let a late `delivered` clobber a stored `read`.
        where=_rank_expr(WhatsAppMessageStatus.status)
        <= _rank_expr(stmt.excluded.status),
    )
    await session.execute(stmt)
    await session.flush()
    row = await session.get(WhatsAppMessageStatus, wamid)
    assert row is not None
    return row


async def unreachable_recipients(
    session: AsyncSession,
    *,
    since: datetime,
    min_failures: int,
) -> list[dict[str, Any]]:
    """Recipients whose outbound deliveries are persistently failing.

    Returns one dict per recipient that, within the ``since`` window, has at
    least ``min_failures`` rows in terminal ``failed`` status AND zero rows
    in ``delivered`` / ``read``. These are "silent patients" — a deactivated
    WhatsApp account, a blocked number, or a number that changed hands — whom
    the bot can no longer reach, so a human needs to re-establish contact.

    Each dict: ``{recipient_id, failed, last_error_code, last_error_title,
    last_failed_at}``. A single grouped scan over the window (one row per
    ``wamid``, since the table is keyed by message id and status advances in
    place via upsert).

    The ``delivered == 0`` guard is what distinguishes a genuinely unreachable
    patient from one who just had a few transient failures among successful
    sends — we don't want to page ops for the latter.
    """
    delivered_filter = WhatsAppMessageStatus.status.in_(("delivered", "read"))
    failed_filter = WhatsAppMessageStatus.status == "failed"
    stmt = (
        select(
            WhatsAppMessageStatus.recipient_id.label("recipient_id"),
            func.count().filter(failed_filter).label("failed"),
            func.count().filter(delivered_filter).label("delivered"),
            func.max(WhatsAppMessageStatus.updated_at)
            .filter(failed_filter)
            .label("last_failed_at"),
        )
        .where(WhatsAppMessageStatus.updated_at >= since)
        .where(WhatsAppMessageStatus.recipient_id.isnot(None))
        .group_by(WhatsAppMessageStatus.recipient_id)
        .having(func.count().filter(failed_filter) >= min_failures)
        .having(func.count().filter(delivered_filter) == 0)
    )
    rows = (await session.execute(stmt)).all()
    out: list[dict[str, Any]] = []
    for r in rows:
        # Pull the most recent failure's error code/title for the ticket note.
        err = (
            await session.execute(
                select(
                    WhatsAppMessageStatus.error_code,
                    WhatsAppMessageStatus.error_title,
                )
                .where(WhatsAppMessageStatus.recipient_id == r.recipient_id)
                .where(failed_filter)
                .where(WhatsAppMessageStatus.updated_at >= since)
                .order_by(WhatsAppMessageStatus.updated_at.desc())
                .limit(1)
            )
        ).first()
        out.append(
            {
                "recipient_id": r.recipient_id,
                "failed": r.failed,
                "last_error_code": err.error_code if err else None,
                "last_error_title": err.error_title if err else None,
                "last_failed_at": r.last_failed_at,
            }
        )
    return out


async def has_recent_success(
    session: AsyncSession, *, recipient_id: str, since: datetime
) -> bool:
    """True iff this recipient has any delivered/read status since ``since``.
    Used by the reconciliation sweep to auto-resolve an unreachable ticket
    once the patient is reachable again."""
    stmt = (
        select(func.count())
        .select_from(WhatsAppMessageStatus)
        .where(WhatsAppMessageStatus.recipient_id == recipient_id)
        .where(WhatsAppMessageStatus.status.in_(("delivered", "read")))
        .where(WhatsAppMessageStatus.updated_at >= since)
    )
    return ((await session.execute(stmt)).scalar_one() or 0) > 0


async def recent(
    session: AsyncSession,
    *,
    recipient_id: str | None = None,
    limit: int = 100,
) -> list[WhatsAppMessageStatus]:
    stmt = (
        select(WhatsAppMessageStatus)
        .order_by(WhatsAppMessageStatus.updated_at.desc())
        .limit(limit)
    )
    if recipient_id is not None:
        stmt = stmt.where(WhatsAppMessageStatus.recipient_id == recipient_id)
    return list((await session.execute(stmt)).scalars().all())

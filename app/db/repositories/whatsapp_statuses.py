from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import case, select
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

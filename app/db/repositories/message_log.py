from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MessageDirection, MessageLog, MessagePayloadKind


async def append_inbound(
    session: AsyncSession,
    *,
    patient_id: str | None,
    payload: dict[str, Any],
    occurred_at: datetime | None = None,
) -> MessageLog:
    row = MessageLog(
        direction=MessageDirection.inbound,
        patient_id=patient_id,
        payload_kind=None,
        message=payload,
        occurred_at=occurred_at or datetime.now(timezone.utc),
    )
    session.add(row)
    await session.flush()
    return row


async def append_outbound(
    session: AsyncSession,
    *,
    patient_id: str | None,
    payload_kind: str,
    payload: dict[str, Any],
    occurred_at: datetime | None = None,
    wamid: str | None = None,
) -> MessageLog:
    """Append an outbound row to the message log.

    ``wamid`` should be passed when Meta accepted the send (which is
    almost every successful path). Storing it as a first-class column
    — not just inside ``payload._wamid`` — is what lets delivery-rate
    queries join cleanly to ``whatsapp_message_statuses``. Failed
    sends (``_send_error`` set, no wamid) leave the column NULL and
    show up in metrics as ``failed_pre_meta``.
    """
    if payload_kind not in {"template", "freeform"}:
        raise ValueError(f"invalid payload_kind: {payload_kind}")
    row = MessageLog(
        direction=MessageDirection.outbound,
        patient_id=patient_id,
        payload_kind=MessagePayloadKind(payload_kind),
        message=payload,
        occurred_at=occurred_at or datetime.now(timezone.utc),
        wamid=wamid,
    )
    session.add(row)
    await session.flush()
    return row


async def recent(session: AsyncSession, *, limit: int = 1000) -> list[MessageLog]:
    stmt = select(MessageLog).order_by(desc(MessageLog.occurred_at)).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


def to_log_dict(row: MessageLog) -> dict[str, Any]:
    """Render a MessageLog row in the legacy MESSAGE_LOG dict shape."""
    if row.direction == MessageDirection.inbound:
        return {
            "direction": "inbound",
            "received_at": row.occurred_at.isoformat(),
            "message": row.message,
        }
    return {
        "direction": "outbound",
        "sent_at": row.occurred_at.isoformat(),
        "payload_type": row.payload_kind.value if row.payload_kind else None,
        "message": row.message,
    }

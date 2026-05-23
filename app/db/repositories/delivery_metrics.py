"""Delivery-rate roll-up queries.

Joins ``message_log`` (outbound rows) to ``whatsapp_message_statuses``
on ``wamid`` to surface what happened to each outbound message after
we handed it to Meta. The numbers feed the ops dashboard tile.

Categories rolled into:

    delivered          — Meta confirmed delivery (status=delivered or read).
    read               — Meta confirmed the recipient opened it.
    sent_only          — Meta accepted the send but no delivered/read
                         webhook has arrived yet (still in flight).
    failed             — Meta accepted then later sent a `failed`
                         status (delivery error: blocked, opted-out,
                         re-engagement window expired, etc).
    failed_pre_meta    — The send was rejected before Meta could
                         issue a wamid (network blip, malformed
                         template, auth failure). We log the row but
                         it has no status to look up.
    no_status_yet      — Edge case: wamid present, no status row yet.
                         Could be a brand-new send the webhook hasn't
                         caught up to, or a webhook delivery delay.

A separate ``failed`` and ``failed_pre_meta`` is intentional — they
have different operational meanings. ``failed`` means the recipient
state is the problem; ``failed_pre_meta`` means OUR pipeline broke.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    MessageDirection,
    MessageLog,
    MessagePayloadKind,
    WhatsAppMessageStatus,
)


def _default_since(now: datetime | None = None) -> datetime:
    return (now or datetime.now(timezone.utc)) - timedelta(hours=24)


def _bucket_of(status: str | None, has_wamid: bool, has_send_error: bool) -> str:
    """Map the (status, has_wamid, has_send_error) tuple to one of the
    six rollup buckets defined in the module docstring. Used both by
    Python-side aggregation in the test fixtures AND in tests' direct
    calls — keep this synced with the SQL ``CASE`` below."""
    if not has_wamid and has_send_error:
        return "failed_pre_meta"
    if status == "failed":
        return "failed"
    if status == "read":
        return "read"
    if status == "delivered":
        return "delivered"
    if status == "sent":
        return "sent_only"
    return "no_status_yet"


# Column expression that mirrors ``_bucket_of`` server-side. Used in
# the GROUP BY queries below so we can roll up in a single round-trip
# instead of fetching every row.
def _bucket_expr():
    has_send_error = MessageLog.message["_send_error"].isnot(None)
    return case(
        (
            (MessageLog.wamid.is_(None)) & has_send_error,
            "failed_pre_meta",
        ),
        (WhatsAppMessageStatus.status == "failed", "failed"),
        (WhatsAppMessageStatus.status == "read", "read"),
        (WhatsAppMessageStatus.status == "delivered", "delivered"),
        (WhatsAppMessageStatus.status == "sent", "sent_only"),
        else_="no_status_yet",
    )


_ZERO_BY_STATUS: dict[str, int] = {
    "delivered": 0,
    "read": 0,
    "sent_only": 0,
    "failed": 0,
    "failed_pre_meta": 0,
    "no_status_yet": 0,
}


async def delivery_summary(
    session: AsyncSession, *, since: datetime | None = None
) -> dict[str, Any]:
    """Single-pass rollup of outbound delivery state since ``since``.

    Returns:

        {
            "since": <iso UTC>,
            "total_outbound": int,
            "by_status": {<bucket>: int, ...},
            "delivery_rate": float,   # (delivered+read) / total
            "failure_rate":  float,   # (failed+failed_pre_meta) / total
            "by_payload_kind": {
                "template": {"total": int, "delivered": int, "failed": int},
                "freeform": {...},
            },
            "top_failure_codes": [{"code": int, "title": str, "count": int}, ...],
        }

    ``delivery_rate`` counts ``read`` as delivered (read implies
    delivered). ``failure_rate`` includes both Meta-side failures and
    our pre-Meta send failures because both are operationally
    actionable.
    """
    since = since or _default_since()
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    bucket = _bucket_expr()

    # Single query: outbound rows since ``since`` LEFT JOINed to
    # whatsapp_message_statuses, grouped by bucket + payload_kind.
    rows = (
        await session.execute(
            select(
                bucket.label("bucket"),
                MessageLog.payload_kind,
                func.count().label("n"),
            )
            .select_from(MessageLog)
            .outerjoin(
                WhatsAppMessageStatus,
                WhatsAppMessageStatus.wamid == MessageLog.wamid,
            )
            .where(MessageLog.direction == MessageDirection.outbound)
            .where(MessageLog.occurred_at >= since)
            .group_by(bucket, MessageLog.payload_kind)
        )
    ).all()

    by_status: dict[str, int] = dict(_ZERO_BY_STATUS)
    by_payload_kind: dict[str, dict[str, int]] = {
        "template": {"total": 0, "delivered": 0, "failed": 0},
        "freeform": {"total": 0, "delivered": 0, "failed": 0},
    }
    total = 0
    for bucket_name, payload_kind, n in rows:
        n = int(n)
        total += n
        by_status[bucket_name] = by_status.get(bucket_name, 0) + n
        kind = (
            payload_kind.value
            if isinstance(payload_kind, MessagePayloadKind)
            else (payload_kind or "freeform")
        )
        if kind not in by_payload_kind:
            by_payload_kind[kind] = {"total": 0, "delivered": 0, "failed": 0}
        by_payload_kind[kind]["total"] += n
        if bucket_name in ("delivered", "read"):
            by_payload_kind[kind]["delivered"] += n
        elif bucket_name in ("failed", "failed_pre_meta"):
            by_payload_kind[kind]["failed"] += n

    delivered_total = by_status["delivered"] + by_status["read"]
    failed_total = by_status["failed"] + by_status["failed_pre_meta"]
    delivery_rate = round(delivered_total / total, 4) if total else 0.0
    failure_rate = round(failed_total / total, 4) if total else 0.0

    # Top failure codes — separate query because we only care about
    # the rows where status='failed' AND there's an error_code.
    err_rows = (
        await session.execute(
            select(
                WhatsAppMessageStatus.error_code,
                WhatsAppMessageStatus.error_title,
                func.count().label("n"),
            )
            .join(
                MessageLog,
                MessageLog.wamid == WhatsAppMessageStatus.wamid,
            )
            .where(MessageLog.direction == MessageDirection.outbound)
            .where(MessageLog.occurred_at >= since)
            .where(WhatsAppMessageStatus.status == "failed")
            .where(WhatsAppMessageStatus.error_code.isnot(None))
            .group_by(
                WhatsAppMessageStatus.error_code,
                WhatsAppMessageStatus.error_title,
            )
            .order_by(func.count().desc())
            .limit(5)
        )
    ).all()
    top_failure_codes = [
        {"code": int(code), "title": title, "count": int(n)}
        for code, title, n in err_rows
    ]

    return {
        "since": since.isoformat(),
        "total_outbound": total,
        "by_status": by_status,
        "delivery_rate": delivery_rate,
        "failure_rate": failure_rate,
        "by_payload_kind": by_payload_kind,
        "top_failure_codes": top_failure_codes,
    }


async def delivery_summary_by_template(
    session: AsyncSession, *, since: datetime | None = None
) -> list[dict[str, Any]]:
    """Per-template delivery rollup.

    The aggregate ``delivery_summary`` blends every template into one
    number, so a single template silently failing while the rest stay
    healthy is invisible. This breaks the rollup down by
    ``message_log.message->>'template_name'`` so ops can spot "the
    new ``dose_reminder_v2`` is at 0% delivery while v1 is fine".

    Returns a list sorted by total DESC (busiest templates lead). Each
    entry:

        {
            "template_name": str,
            "total": int,
            "delivered": int,           # delivered + read
            "failed": int,              # Meta-side failures
            "failed_pre_meta": int,     # our pipeline failures
            "delivery_rate": float,     # rounded to 4 dp
            "failure_rate": float,
        }

    Only ``payload_kind = template`` rows are included — freeform
    sends don't have a template_name and shouldn't muddy this view.
    Rows missing ``template_name`` (defensive — shouldn't happen
    given the gateway always sets it for template sends) are bucketed
    under ``"<unknown>"`` so they're visible rather than silently
    dropped.
    """
    since = since or _default_since()
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)

    bucket = _bucket_expr()
    # ``MessageLog.message`` is a plain JSON column (not JSONB), so the
    # ``.astext`` shortcut isn't available. Use Postgres'
    # ``json_extract_path_text`` to pull the template_name out as a
    # text scalar — works without casting.
    template_expr = func.json_extract_path_text(
        MessageLog.message, "template_name"
    )

    rows = (
        await session.execute(
            select(
                template_expr.label("template_name"),
                bucket.label("bucket"),
                func.count().label("n"),
            )
            .select_from(MessageLog)
            .outerjoin(
                WhatsAppMessageStatus,
                WhatsAppMessageStatus.wamid == MessageLog.wamid,
            )
            .where(MessageLog.direction == MessageDirection.outbound)
            .where(MessageLog.payload_kind == MessagePayloadKind.template)
            .where(MessageLog.occurred_at >= since)
            .group_by(template_expr, bucket)
        )
    ).all()

    # Pivot the (template, bucket, count) tuples into a dict keyed by
    # template_name. The single-pass dict build keeps us linear in
    # rowcount.
    per_template: dict[str, dict[str, int]] = {}
    for template_name, bucket_name, n in rows:
        key = template_name or "<unknown>"
        slot = per_template.setdefault(
            key,
            {
                "total": 0,
                "delivered": 0,
                "failed": 0,
                "failed_pre_meta": 0,
            },
        )
        n = int(n)
        slot["total"] += n
        if bucket_name in ("delivered", "read"):
            slot["delivered"] += n
        elif bucket_name == "failed":
            slot["failed"] += n
        elif bucket_name == "failed_pre_meta":
            slot["failed_pre_meta"] += n

    # Build the sorted output list with rounded rates.
    out: list[dict[str, Any]] = []
    for name, slot in per_template.items():
        total = slot["total"]
        failed_total = slot["failed"] + slot["failed_pre_meta"]
        out.append(
            {
                "template_name": name,
                "total": total,
                "delivered": slot["delivered"],
                "failed": slot["failed"],
                "failed_pre_meta": slot["failed_pre_meta"],
                "delivery_rate": (
                    round(slot["delivered"] / total, 4) if total else 0.0
                ),
                "failure_rate": (
                    round(failed_total / total, 4) if total else 0.0
                ),
            }
        )
    out.sort(key=lambda r: r["total"], reverse=True)
    return out

"""Per-patient inbound rate limiting.

Without this, a patient (or a misbehaving WhatsApp client doing
infinite retries) can flood the orchestrator with 1000 messages in
30 seconds — burning LLM tokens, firing dispatchers, choking the
queue, and potentially crowding out real patients in the same
shared infra. For a medical bot deployed in production this is
baseline safety.

The gate counts inbound ``message_log`` rows for the patient in a
rolling window. When the count is at or above the configured
limit, the gate fires:

    - ``check_inbound_rate_limit`` returns ``is_limited=True``.
    - The /route endpoint skips graph invocation entirely (no LLM,
      no handlers, no outbound).
    - The inbound is still logged to ``message_log`` for the audit
      trail — we want to be able to forensically reconstruct what
      happened during an incident.
    - An audit row is written with ``reason_codes=["rate_limited"]``.
    - On the FIRST breach per patient per day, an ops ticket opens
      (``inbound_rate_limit`` category) so a human notices. The
      idempotency key is patient_id + UTC date so a sustained
      burst doesn't open a ticket per minute.

Defaults:

    Window  = 5 minutes
    Limit   = 30 inbound messages

A real engaged patient might send 5–10 messages over a few
minutes during an active conversation. 30 in 5 min is well past
the natural ceiling and clearly indicates a loop or abuse. Both
configurable via env vars so the policy can be tuned per-deploy.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MessageDirection, MessageLog

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateLimitResult:
    """Rich return value so the caller can both decide AND log
    structured details. ``count`` is the number of inbound rows in
    the window — useful in the audit row + ops ticket notes for
    root-causing the incident."""

    is_limited: bool
    count: int
    limit: int
    window_minutes: int


def _window_minutes() -> int:
    """Rolling-window span. 5 min by default — short enough that a
    burst flares up quickly, long enough to absorb a multi-message
    natural conversation."""
    try:
        return int(os.getenv("INBOUND_RATE_LIMIT_WINDOW_MINUTES", "5"))
    except ValueError:
        return 5


def _limit() -> int:
    """Inbound count at or above which the gate fires. 30 by default
    — well past the 5–10/conversation natural ceiling."""
    try:
        return int(os.getenv("INBOUND_RATE_LIMIT_COUNT", "30"))
    except ValueError:
        return 30


async def check_inbound_rate_limit(
    db: AsyncSession,
    *,
    patient_phone: str,
    now: datetime | None = None,
) -> RateLimitResult:
    """Return whether the patient should be rate-limited this turn.

    Counts ``message_log`` inbound rows for the patient in the last
    ``window_minutes``. ``is_limited=True`` when count >= limit
    (exclusive of the current inbound — we count what we've already
    logged, then decide whether to process THIS one).

    Defensive on missing phone: returns ``is_limited=False`` so a
    malformed inbound doesn't accidentally short-circuit through
    the gate (it'll fail on something more informative downstream).
    """
    if not patient_phone:
        return RateLimitResult(
            is_limited=False,
            count=0,
            limit=_limit(),
            window_minutes=_window_minutes(),
        )

    when = now or datetime.now(timezone.utc)
    cutoff = when - timedelta(minutes=_window_minutes())

    stmt = (
        select(func.count())
        .select_from(MessageLog)
        .where(MessageLog.direction == MessageDirection.inbound)
        .where(MessageLog.patient_id == patient_phone)
        .where(MessageLog.occurred_at >= cutoff)
    )
    count = int((await db.execute(stmt)).scalar() or 0)
    limit = _limit()
    is_limited = count >= limit

    if is_limited:
        log.warning(
            "inbound rate limit hit: patient=%s count=%d limit=%d "
            "window_min=%d",
            patient_phone,
            count,
            limit,
            _window_minutes(),
        )

    return RateLimitResult(
        is_limited=is_limited,
        count=count,
        limit=limit,
        window_minutes=_window_minutes(),
    )

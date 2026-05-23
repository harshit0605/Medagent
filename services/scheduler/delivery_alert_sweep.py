"""Background sweep that turns the delivery-tracking metrics into
actionable ops tickets when the bot starts failing to deliver.

The dashboard tile we shipped surfaces ``failure_rate`` and the top
failure error codes, but only humans looking at the dashboard see
those. A 50% delivery failure overnight (Meta API outage, expired
auth token, regional issue) would sit silently with red metrics
until someone happened to look. This sweep makes the failures
actionable:

    - Every ``DELIVERY_ALERT_SWEEP_SECONDS`` (default 600s = 10 min)
      we run the same ``delivery_summary`` query the dashboard uses,
      bounded to the last 30 minutes (configurable).
    - If ``failure_rate > FAILURE_THRESHOLD`` AND
      ``total_outbound >= MIN_VOLUME``, we open an
      ``outbound_delivery_burst`` ops ticket. ``MIN_VOLUME`` exists
      so a single failed send out of 1 doesn't trip the alarm —
      we want statistical signal, not single-row noise.
    - Idempotent via ``find_open_for_patient_category`` keyed on a
      synthetic patient_id ``"platform:delivery"`` (these aren't
      patient-specific failures). One ticket open at a time.
    - Auto-resolve when ``failure_rate <= RECOVERY_THRESHOLD``: the
      sweep closes the open ticket with a "metrics recovered" note.
      Without this, a transient outage that recovers would leave
      a stale ticket cluttering the queue.

The patient_id ``"platform:delivery"`` is a convention shared with
``service_health_reconciler`` (which uses ``"platform:..."`` for
its own meta-tickets). The ops queue UI doesn't special-case it —
it just renders as a ticket without a patient link.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import (
    delivery_metrics as delivery_metrics_repo,
    ops_tickets as ops_tickets_repo,
)

log = logging.getLogger(__name__)


# Synthetic patient_id used on the ops_tickets row. The ops queue UI
# renders this as plain text (no patient link) — same convention used
# by service_health_reconciler.
PATIENT_ID: str = "platform:delivery"
CATEGORY: str = "outbound_delivery_burst"
PRIORITY: str = "high"  # delivery outage is patient-impacting
SLA_MINUTES: int = 60   # tight SLA — outages shouldn't sit


def _failure_threshold() -> float:
    """Failure-rate above which we open a ticket. Default 10%. Below
    this, blips and one-off bad sends roll up but don't page anyone."""
    try:
        return float(os.getenv("DELIVERY_FAILURE_THRESHOLD", "0.10"))
    except ValueError:
        return 0.10


def _recovery_threshold() -> float:
    """Failure-rate at or below which we auto-resolve an open burst
    ticket. Distinct from the open-threshold so we have hysteresis —
    we don't want the ticket flickering open/resolved as the rate
    oscillates around 10%."""
    try:
        return float(os.getenv("DELIVERY_RECOVERY_THRESHOLD", "0.05"))
    except ValueError:
        return 0.05


def _min_volume() -> int:
    """Minimum outbound volume in the window before we trust the rate.
    5 sends in 30 min gives a usable denominator without burning ops
    cycles on tests."""
    try:
        return int(os.getenv("DELIVERY_ALERT_MIN_VOLUME", "5"))
    except ValueError:
        return 5


def _window_minutes() -> int:
    """How far back the rolling window looks. 30 min by default — long
    enough that a brief retry storm averages out, short enough that
    a real outage opens a ticket within ~half an hour."""
    try:
        return int(os.getenv("DELIVERY_ALERT_WINDOW_MINUTES", "30"))
    except ValueError:
        return 30


async def sweep_delivery_alerts(
    session: AsyncSession, *, now: datetime | None = None
) -> dict[str, Any]:
    """One pass of the delivery alert sweep. Returns a counter dict
    the scheduler logs as a heartbeat detail.

    Caller commits — the loop wrapper in ``services.scheduler.main``
    commits after the call returns.
    """
    when = now or datetime.now(timezone.utc)
    window_start = when - timedelta(minutes=_window_minutes())
    summary = await delivery_metrics_repo.delivery_summary(
        session, since=window_start
    )

    total = summary["total_outbound"]
    failure_rate = summary["failure_rate"]
    by_status = summary["by_status"]
    failed_total = by_status.get("failed", 0) + by_status.get(
        "failed_pre_meta", 0
    )

    existing = await ops_tickets_repo.find_open_for_patient_category(
        session, patient_id=PATIENT_ID, category=CATEGORY
    )

    out: dict[str, Any] = {
        "total_outbound": total,
        "failure_rate": failure_rate,
        "failed": failed_total,
        "opened": False,
        "auto_resolved": False,
        "min_volume_skipped": False,
    }

    # Below the volume floor — no statistical signal. Don't open a
    # ticket and don't resolve an existing one (a 1-of-3 failure
    # while the previous-window ticket is open shouldn't immediately
    # count as recovered; ride out the noise).
    if total < _min_volume():
        out["min_volume_skipped"] = True
        return out

    if failure_rate > _failure_threshold():
        if existing is None:
            top_codes = ", ".join(
                f"{e['code']}({e['count']})"
                for e in summary["top_failure_codes"][:3]
            ) or "no error codes captured"
            notes = (
                f"Outbound delivery failure rate "
                f"{failure_rate * 100:.1f}% over the last "
                f"{_window_minutes()} min "
                f"({failed_total} of {total} sends failed). "
                f"Top error codes: {top_codes}. "
                f"Pre-Meta failures: "
                f"{by_status.get('failed_pre_meta', 0)}; "
                f"Meta-side failures: {by_status.get('failed', 0)}."
            )
            ticket = await ops_tickets_repo.create(
                session,
                patient_id=PATIENT_ID,
                category=CATEGORY,
                priority=PRIORITY,
                sla_minutes=SLA_MINUTES,
                notes=notes,
            )
            out["opened"] = True
            out["ticket_id"] = ticket.id
            log.warning(
                "delivery alert: opened ticket %s — failure_rate=%.2f%% "
                "(%d/%d sends failed)",
                ticket.id,
                failure_rate * 100,
                failed_total,
                total,
            )
        else:
            # Already alerted — append a fresh note so the timeline
            # shows the alarm is still live (and ops can see whether
            # it's getting worse or better since the last sweep).
            note = (
                f"still elevated: failure_rate={failure_rate * 100:.1f}% "
                f"({failed_total}/{total} sends in last "
                f"{_window_minutes()} min)"
            )
            await ops_tickets_repo.append_note(
                session,
                existing.id,
                actor="system",
                note=note,
            )
            out["ticket_id"] = existing.id
        return out

    if failure_rate <= _recovery_threshold() and existing is not None:
        # Recovered — auto-resolve so the queue doesn't accumulate
        # stale alarm tickets. Ops can re-open if they want a deeper
        # look at the prior incident.
        await ops_tickets_repo.resolve(
            session,
            existing.id,
            actor="system",
            notes=(
                f"auto-resolved: failure_rate recovered to "
                f"{failure_rate * 100:.1f}% "
                f"({failed_total}/{total} in last "
                f"{_window_minutes()} min)"
            ),
        )
        out["auto_resolved"] = True
        out["ticket_id"] = existing.id
        log.info(
            "delivery alert: auto-resolved ticket %s — failure_rate "
            "recovered to %.2f%%",
            existing.id,
            failure_rate * 100,
        )

    return out

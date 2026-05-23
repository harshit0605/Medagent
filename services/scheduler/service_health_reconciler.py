"""Service-health reconciler — observability auto-ticketing.

Looks at the same data the ``/ops/health`` page shows and, when
something crosses a threshold, opens an idempotent ops_ticket so a
human is paged. Three conditions today:

  - **stale_component**: a heartbeat is older than its expected
    cadence (per-component thresholds mirror the ones in
    ``services/orchestrator/main.py``). Implies the loop has stopped
    ticking entirely.
  - **errored_component**: ≥ ``ERROR_RUN_THRESHOLD`` (default 3)
    consecutive errors. Loop is up but failing.
  - **failed_event_burst**: ≥ ``FAILED_BURST_THRESHOLD`` (default 5)
    failed ``scheduled_events`` rows in the last 30 min. Could be
    Meta rejection, gateway outage, payload bug — anything blocking
    delivery in volume.

Idempotency: ``ops_tickets_repo.find_open_for_patient_category`` is
keyed by (patient_id, category), and we use synthetic patient_id
``service:<component-or-burst-key>`` so each condition collapses to a
single open ticket. Once that ticket is resolved, a recurrence opens
a new one.

Cheap by design — read-only DB scan plus at most one ticket-create
per condition per pass. Safe to call as often as the dispatcher loop.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ScheduledEvent, ScheduledEventStatus, ServiceHeartbeat
from app.db.repositories import ops_tickets as ops_tickets_repo

log = logging.getLogger(__name__)


# Per-component "we expected a heartbeat by now" thresholds. Mirrors
# ``_STALE_THRESHOLD_SECONDS`` in services/orchestrator/main.py — the
# ops console page and the auto-ticket reconciler must agree on what
# "stale" means or operators will see one signal flip without the
# other.
STALE_THRESHOLD_SECONDS: dict[str, int] = {
    "scheduler.dispatch": 180,
    "scheduler.dose_materialize": 1800,
    "scheduler.missed_dose_sweep": 900,
    "scheduler.recap_sweep": 1800,
    "scheduler.care_gap_sweep": 86400,
}
DEFAULT_STALE_THRESHOLD = 3600

ERROR_RUN_THRESHOLD = 3
FAILED_BURST_WINDOW = timedelta(minutes=30)
FAILED_BURST_THRESHOLD = 5

TICKET_CATEGORY = "service_health"
TICKET_PRIORITY = "p1"
TICKET_SLA_MINUTES = 60  # 1h — operator must look quickly


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _stale_seconds_for(component: str) -> int:
    return STALE_THRESHOLD_SECONDS.get(component, DEFAULT_STALE_THRESHOLD)


async def _ensure_ticket(
    db: AsyncSession,
    *,
    synthetic_patient_id: str,
    notes: str,
) -> bool:
    """Open exactly one ``service_health`` ticket per
    ``synthetic_patient_id`` at a time. Returns True if a new ticket
    was created, False if one is already open."""
    existing = await ops_tickets_repo.find_open_for_patient_category(
        db,
        patient_id=synthetic_patient_id,
        category=TICKET_CATEGORY,
    )
    if existing is not None:
        return False
    await ops_tickets_repo.create(
        db,
        patient_id=synthetic_patient_id,
        category=TICKET_CATEGORY,
        priority=TICKET_PRIORITY,
        sla_minutes=TICKET_SLA_MINUTES,
        notes=notes,
    )
    return True


async def _reconcile_heartbeat(
    db: AsyncSession,
    *,
    heartbeat: ServiceHeartbeat,
    now: datetime,
) -> dict[str, int]:
    """One heartbeat row → at most one ticket. Returns counters."""
    out = {"stale_opened": 0, "errored_opened": 0}
    last_run = _ensure_utc(heartbeat.last_run_at)
    seconds_since = (now - last_run).total_seconds()
    threshold = _stale_seconds_for(heartbeat.component)

    if seconds_since > threshold:
        opened = await _ensure_ticket(
            db,
            synthetic_patient_id=f"service:{heartbeat.component}:stale",
            notes=(
                f"Component {heartbeat.component} hasn't tick'd in "
                f"{int(seconds_since)}s "
                f"(threshold={threshold}s). Last outcome: "
                f"{heartbeat.last_outcome}. Last run at {last_run.isoformat()}."
            ),
        )
        if opened:
            out["stale_opened"] += 1
    elif (
        heartbeat.last_outcome == "error"
        and (heartbeat.consecutive_errors or 0) >= ERROR_RUN_THRESHOLD
    ):
        opened = await _ensure_ticket(
            db,
            synthetic_patient_id=f"service:{heartbeat.component}:errors",
            notes=(
                f"Component {heartbeat.component} has "
                f"{heartbeat.consecutive_errors} consecutive errored "
                f"runs. Last run at {last_run.isoformat()}; details: "
                f"{heartbeat.details!r}."
            ),
        )
        if opened:
            out["errored_opened"] += 1
    return out


async def _failed_event_burst_count(
    db: AsyncSession, *, since: datetime
) -> int:
    """Count of ``scheduled_events`` rows that flipped to ``failed``
    inside the burst window. We approximate ``flipped at`` with
    ``created_at`` because we don't store a separate failed_at column
    today; events that were created in the window AND ended up
    failed are the noise we care about. Off-window events that
    failed earlier are irrelevant to a current burst."""
    stmt = (
        select(func.count(ScheduledEvent.id))
        .where(ScheduledEvent.status == ScheduledEventStatus.failed)
        .where(ScheduledEvent.created_at >= since)
    )
    return (await db.execute(stmt)).scalar_one()


async def reconcile_service_health(
    db: AsyncSession, *, now: datetime | None = None
) -> dict[str, int]:
    """One pass. Returns counters so the caller / log can see what
    the reconciler did. Caller commits."""
    when_now = _ensure_utc(now or datetime.now(timezone.utc))
    out = {
        "components_examined": 0,
        "stale_opened": 0,
        "errored_opened": 0,
        "failed_burst_opened": 0,
    }

    # 1. Per-heartbeat reconciliation.
    rows = (
        await db.execute(
            select(ServiceHeartbeat).order_by(ServiceHeartbeat.component)
        )
    ).scalars().all()
    for hb in rows:
        out["components_examined"] += 1
        sub = await _reconcile_heartbeat(db, heartbeat=hb, now=when_now)
        out["stale_opened"] += sub["stale_opened"]
        out["errored_opened"] += sub["errored_opened"]

    # 2. Failed-event burst detection.
    burst_since = when_now - FAILED_BURST_WINDOW
    burst_count = await _failed_event_burst_count(db, since=burst_since)
    if burst_count >= FAILED_BURST_THRESHOLD:
        opened = await _ensure_ticket(
            db,
            synthetic_patient_id="service:scheduled_events:failed_burst",
            notes=(
                f"{burst_count} scheduled_events flipped to failed in "
                f"the last {int(FAILED_BURST_WINDOW.total_seconds() / 60)} "
                f"min (threshold={FAILED_BURST_THRESHOLD}). Inspect via "
                f"GET /scheduled-events?status=failed."
            ),
        )
        if opened:
            out["failed_burst_opened"] += 1

    if any(out.get(k, 0) > 0 for k in ("stale_opened", "errored_opened", "failed_burst_opened")):
        await db.flush()
    return out

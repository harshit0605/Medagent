"""Background sweep that re-pages unacked critical alerts.

When the orchestrator creates a critical alert it enqueues a
single ``clinical_alert_page`` event for the dispatcher to
fire. If the chosen doctor doesn't acknowledge the alert
within ``REPAGE_AFTER_SECONDS`` (default 5 min), this sweep
enqueues another paging event so the same doctor gets pinged
again — up to ``MAX_PAGE_ATTEMPTS`` (default 3) total.

Why a sweep instead of pre-enqueueing T+5m, T+10m, T+15m
events at create time?

    1. Acknowledgements cancel re-pages cleanly. The sweep
       skips alerts in ``acknowledged`` or ``resolved`` state,
       so an early ack means no extra pings without us having
       to track-and-cancel pre-scheduled events.
    2. The cap key (``paged_attempts``) lives on the alert
       row, not on individual events — easier to reason about
       in one place.

Sweep cadence: every 60s. Cheap query (indexed on status +
severity + paged_at via the model's compound index). Does not
talk to the gateway directly — just enqueues paging events
for the dispatcher to pick up on the next tick.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import clinical_alerts as clinical_alerts_repo
from app.db.repositories import scheduled_events as scheduled_events_repo
from services.orchestrator import clinical_alert_pager

log = logging.getLogger(__name__)


async def sweep_repages(
    session: AsyncSession, *, now: datetime | None = None
) -> dict[str, Any]:
    """One pass of the re-page sweep. Returns a counter dict
    the scheduler logs as a heartbeat detail.

    Caller commits — the wrapper in ``services.scheduler.main``
    commits after this returns.
    """
    when = now or datetime.now(timezone.utc)
    candidates = await clinical_alerts_repo.list_unacked_for_repage(
        session,
        older_than_seconds=clinical_alert_pager.REPAGE_AFTER_SECONDS,
        max_attempts=clinical_alert_pager.MAX_PAGE_ATTEMPTS,
    )
    repaged_ids: list[int] = []
    for alert in candidates:
        try:
            await scheduled_events_repo.enqueue(
                session,
                event_type="clinical_alert_page",
                patient_id=alert.patient_phone or "",
                payload={
                    "alert_id": alert.id,
                    "patient_db_id": alert.patient_id,
                    "severity": alert.severity,
                    "repage": True,
                },
                scheduled_for=when,
            )
            repaged_ids.append(alert.id)
            log.info(
                "clinical alert %s re-page enqueued "
                "(attempt %d / %d)",
                alert.id,
                (alert.paged_attempts or 0) + 1,
                clinical_alert_pager.MAX_PAGE_ATTEMPTS,
            )
        except Exception:  # noqa: BLE001 — keep the sweep alive
            log.exception(
                "clinical alert %s re-page enqueue failed",
                alert.id,
            )
    return {
        "candidates": len(candidates),
        "repaged": len(repaged_ids),
        "repaged_ids": repaged_ids,
    }

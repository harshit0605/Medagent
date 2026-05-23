"""Background sweep that marks ops tickets crossing their SLA window.

Every ops ticket carries an ``sla_minutes`` budget. The DTO layer
exposes a live ``is_overdue`` boolean that re-evaluates per request,
but that's not enough to:

    1. Audit the breach   — we want a durable note + timestamp on the
                            row, not just a render-time computation.
    2. Quantify breaches   — analytics needs ``COUNT(*) WHERE
                            sla_breached_at IS NOT NULL`` partitioned
                            by category, which requires a stamped
                            column (live booleans aren't queryable
                            historically).

This sweep runs every ``SLA_BREACH_SWEEP_SECONDS`` (default 600s = 10
min). Each pass:

    - Loads ``find_breach_candidates`` — open/acked, not snoozed, past
      SLA, not yet breached.
    - Stamps ``sla_breached_at = now()`` on each via ``mark_sla_breached``
      which also appends a ``[SLA breached]`` note.

Snoozed tickets are excluded while the snooze is active — they're
out of the active queue, so the SLA clock effectively pauses. Once
the snooze clears, the next sweep catches them based on raw age.

The sweep is idempotent: ``mark_sla_breached`` no-ops on tickets
already marked, so a long sweep cadence + multiple worker replicas
won't double-mark.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import ops_tickets as ops_tickets_repo

log = logging.getLogger(__name__)


async def sweep_sla_breaches(
    session: AsyncSession, *, now: datetime | None = None
) -> dict[str, Any]:
    """One pass of the breach sweep. Returns a counter dict the
    scheduler logs as a heartbeat detail.

    Caller is responsible for commit — the loop wrapper in
    ``services.scheduler.main`` commits after the call returns.
    """
    when = now or datetime.now(timezone.utc)
    candidates = await ops_tickets_repo.find_breach_candidates(
        session, now=when
    )
    breached_ids: list[int] = []
    breached_categories: dict[str, int] = {}
    for ticket in candidates:
        # mark_sla_breached is idempotent: a ticket marked between
        # the candidate fetch and now (e.g. by a concurrent sweep)
        # leaves ``sla_breached_at`` untouched and returns the row.
        marked = await ops_tickets_repo.mark_sla_breached(
            session, ticket.id, when=when
        )
        if marked is None:
            continue
        # Confirm we actually stamped this row in THIS call — older
        # marks (from a prior pass) shouldn't bump the counters.
        if marked.sla_breached_at == when:
            breached_ids.append(marked.id)
            breached_categories[marked.category] = (
                breached_categories.get(marked.category, 0) + 1
            )
            log.warning(
                "ops ticket %s breached SLA "
                "(category=%s, sla_minutes=%d, patient_id=%s)",
                marked.id,
                marked.category,
                marked.sla_minutes,
                marked.patient_id,
            )
    return {
        "candidates": len(candidates),
        "breached": len(breached_ids),
        "breached_ticket_ids": breached_ids,
        "breached_by_category": breached_categories,
    }

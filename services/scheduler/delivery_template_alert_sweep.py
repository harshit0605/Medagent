"""Per-template delivery alert sweep.

The existing ``delivery_alert_sweep`` watches the AGGREGATE outbound
failure rate. That misses the case the per-template breakdown was
designed to catch: one template (e.g. a freshly-rolled v2) at 100%
failure while the rest stay healthy. Aggregate looks fine, but
patients on that specific reminder track silently get nothing.

This sweep walks the same ``delivery_summary_by_template`` rollup the
dashboard table renders, and opens an idempotent
``outbound_template_burst`` ticket per failing template:

    - Synthetic patient_id = ``platform:template:<name>`` so each
      template gets its own ticket lane (idempotency key) and ops
      can see at a glance which template is broken.
    - Opens when ``failure_rate > FAILURE_THRESHOLD`` AND
      ``total >= MIN_VOLUME`` (lower than the aggregate floor —
      a quiet template may only see 3-5 sends per window, but
      a 100% failure rate on those 3 still warrants a flag).
    - Re-notes the existing ticket on subsequent passes while still
      elevated.
    - Auto-resolves when ``failure_rate <= RECOVERY_THRESHOLD``
      (hysteresis to prevent flicker).

This sweep and ``delivery_alert_sweep`` can both fire on the same
underlying outage. That's intentional — the aggregate ticket says
"the bot is broken in general" while the per-template tickets say
"these specific templates are the problem". Ops can resolve them
together when they fix the root cause.
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


CATEGORY: str = "outbound_template_burst"
PRIORITY: str = "high"
SLA_MINUTES: int = 60


def _patient_id_for_template(name: str) -> str:
    """Synthetic patient_id keyed by template name. The ops queue
    UI renders this as plain text (no patient link); the colon-prefix
    matches the convention used by ``service_health_reconciler`` and
    ``delivery_alert_sweep`` (``platform:...``). Length-bounded to
    fit the OpsTicket.patient_id String(128) column even with very
    long template names."""
    return f"platform:template:{name}"[:128]


def _failure_threshold() -> float:
    """Failure-rate above which a template alert opens. Same default
    as the aggregate sweep (10%) — easy to harmonise alerting policy
    across the two sweeps. Override per-template via
    ``DELIVERY_TEMPLATE_FAILURE_THRESHOLD`` if you want a tighter
    standard for individual templates."""
    try:
        return float(
            os.getenv("DELIVERY_TEMPLATE_FAILURE_THRESHOLD", "0.10")
        )
    except ValueError:
        return 0.10


def _recovery_threshold() -> float:
    """Failure-rate at or below which an open template ticket auto-
    resolves. Hysteresis below the open threshold prevents a
    template oscillating around 10% from flickering open/resolved."""
    try:
        return float(
            os.getenv("DELIVERY_TEMPLATE_RECOVERY_THRESHOLD", "0.05")
        )
    except ValueError:
        return 0.05


def _min_volume() -> int:
    """Minimum sends-in-window before we trust a template's rate.
    Lower than the aggregate's 5 — a quiet template (e.g. a
    seldom-used recap variant) might see only 3 sends per 30-minute
    window, and 100% failure on those 3 is still worth a ticket.
    Below this floor, single-blip noise is suppressed."""
    try:
        return int(os.getenv("DELIVERY_TEMPLATE_ALERT_MIN_VOLUME", "3"))
    except ValueError:
        return 3


def _window_minutes() -> int:
    """Rolling-window span for the per-template summary. Matches the
    aggregate sweep so the two cadences align — easier mental model
    when both fire on the same outage."""
    try:
        return int(
            os.getenv("DELIVERY_TEMPLATE_ALERT_WINDOW_MINUTES", "30")
        )
    except ValueError:
        return 30


async def sweep_template_alerts(
    session: AsyncSession, *, now: datetime | None = None
) -> dict[str, Any]:
    """One pass of the per-template alert sweep. Walks the
    delivery-by-template rollup, opens / re-notes / auto-resolves
    a ticket per template based on its rolling failure rate.

    Caller commits — the loop wrapper in
    ``services.scheduler.main`` commits after the call returns.
    """
    when = now or datetime.now(timezone.utc)
    window_start = when - timedelta(minutes=_window_minutes())
    rows = await delivery_metrics_repo.delivery_summary_by_template(
        session, since=window_start
    )

    failure_threshold = _failure_threshold()
    recovery_threshold = _recovery_threshold()
    min_volume = _min_volume()

    opened: list[str] = []
    auto_resolved: list[str] = []
    re_noted: list[str] = []
    skipped_low_volume: list[str] = []

    for row in rows:
        name = row["template_name"]
        total = int(row["total"])
        failure_rate = float(row["failure_rate"])
        failed_total = int(row["failed"]) + int(row["failed_pre_meta"])
        synth_patient = _patient_id_for_template(name)

        existing = await ops_tickets_repo.find_open_for_patient_category(
            session, patient_id=synth_patient, category=CATEGORY
        )

        # Below the volume floor: don't open new tickets, but DON'T
        # auto-resolve existing ones either — the timer pauses for
        # quiet templates. Once volume returns we'll re-evaluate.
        if total < min_volume:
            skipped_low_volume.append(name)
            continue

        if failure_rate > failure_threshold:
            if existing is None:
                notes = (
                    f"Template {name}: failure rate "
                    f"{failure_rate * 100:.1f}% over the last "
                    f"{_window_minutes()} min "
                    f"({failed_total} of {total} sends failed). "
                    f"Pre-Meta failures: "
                    f"{row.get('failed_pre_meta', 0)}; "
                    f"Meta-side failures: {row.get('failed', 0)}."
                )
                ticket = await ops_tickets_repo.create(
                    session,
                    patient_id=synth_patient,
                    category=CATEGORY,
                    priority=PRIORITY,
                    sla_minutes=SLA_MINUTES,
                    notes=notes,
                )
                opened.append(name)
                log.warning(
                    "delivery template alert: opened ticket %s for "
                    "template %s — failure_rate=%.2f%% (%d/%d sends)",
                    ticket.id,
                    name,
                    failure_rate * 100,
                    failed_total,
                    total,
                )
            else:
                note = (
                    f"still elevated: {failure_rate * 100:.1f}% "
                    f"({failed_total}/{total} sends in last "
                    f"{_window_minutes()} min)"
                )
                await ops_tickets_repo.append_note(
                    session,
                    existing.id,
                    actor="system",
                    note=note,
                )
                re_noted.append(name)
            continue

        if (
            failure_rate <= recovery_threshold
            and existing is not None
        ):
            await ops_tickets_repo.resolve(
                session,
                existing.id,
                actor="system",
                notes=(
                    f"auto-resolved: failure rate recovered to "
                    f"{failure_rate * 100:.1f}% "
                    f"({failed_total}/{total} in last "
                    f"{_window_minutes()} min)"
                ),
            )
            auto_resolved.append(name)
            log.info(
                "delivery template alert: auto-resolved ticket %s "
                "for template %s — failure_rate=%.2f%%",
                existing.id,
                name,
                failure_rate * 100,
            )

    return {
        "templates_evaluated": len(rows),
        "opened": len(opened),
        "opened_templates": opened,
        "auto_resolved": len(auto_resolved),
        "auto_resolved_templates": auto_resolved,
        "re_noted": len(re_noted),
        "re_noted_templates": re_noted,
        "skipped_low_volume": len(skipped_low_volume),
    }

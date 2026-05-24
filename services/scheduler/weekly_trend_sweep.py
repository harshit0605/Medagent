"""Proactive weekly self-report trend push.

The vitals self-report flow (P2) logs readings and acks each one inline. This
sweep closes the retention loop the SoT calls for: once a week, patients who've
logged readings get a short "your week in numbers" summary — momentum without
them having to ask.

Per patient with ``patient_self_report`` observations in the last 7 days, we
enqueue ONE ``weekly_trend_push`` event (deduped so a patient gets at most one
per 7-day window). The dispatcher renders it freeform (in-CSW) or via the
``weekly_trend_v1`` template (out-of-CSW).

Opt-in: the scheduler only runs the loop when ``WEEKLY_TREND_PUSH_ENABLED=1``;
the sweep function itself is always callable (and unit-tested).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import distinct, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MetricObservation, Patient, ScheduledEvent
from app.db.repositories import scheduled_events as scheduled_events_repo

log = logging.getLogger(__name__)

WEEKLY_TREND_EVENT_TYPE = "weekly_trend_push"

# metric_key → (friendly label, unit). Unknown keys fall back to the raw key.
_METRIC_LABELS: dict[str, tuple[str, str]] = {
    "blood_glucose": ("Blood glucose", "mg/dL"),
    "bp_systolic": ("Systolic BP", "mmHg"),
    "bp_diastolic": ("Diastolic BP", "mmHg"),
    "hba1c": ("HbA1c", "%"),
    "weight_kg": ("Weight", "kg"),
    "peak_flow": ("Peak flow", "L/min"),
    "rescue_inhaler_puffs": ("Reliever puffs", "puffs"),
}


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def summarize_observations(
    observations: list[MetricObservation],
) -> list[dict[str, Any]]:
    """Group newest-first observations into per-metric {label, count, latest}.
    Pure helper so the summary logic is unit-testable."""
    by_metric: dict[str, dict[str, Any]] = {}
    for o in observations:
        slot = by_metric.setdefault(
            o.metric_key, {"count": 0, "latest": None}
        )
        slot["count"] += 1
        if slot["latest"] is None:  # newest-first → first seen is the latest
            slot["latest"] = o.value
    out: list[dict[str, Any]] = []
    for key, agg in by_metric.items():
        label, unit = _METRIC_LABELS.get(key, (key, ""))
        latest = agg["latest"]
        out.append(
            {
                "metric_key": key,
                "label": label,
                "unit": unit,
                "count": agg["count"],
                "latest": str(
                    latest if not isinstance(latest, Decimal) else latest
                ),
            }
        )
    return out


async def _recently_pushed(
    db: AsyncSession, *, phone: str, cutoff: datetime
) -> bool:
    stmt = (
        select(ScheduledEvent)
        .where(
            ScheduledEvent.patient_id == phone,
            ScheduledEvent.event_type == WEEKLY_TREND_EVENT_TYPE,
            ScheduledEvent.created_at >= cutoff,
        )
        .limit(1)
    )
    return (await db.execute(stmt)).scalars().first() is not None


async def sweep_weekly_trends(
    db: AsyncSession, *, now: datetime | None = None, lookback_days: int = 7
) -> dict[str, int]:
    """One pass: enqueue a weekly-trend push for each patient with recent
    self-reported readings (deduped to once per ``lookback_days``)."""
    when = _aware(now or datetime.now(timezone.utc))
    cutoff = when - timedelta(days=lookback_days)

    cand_stmt = select(distinct(MetricObservation.patient_id)).where(
        MetricObservation.source == "patient_self_report",
        MetricObservation.observed_at >= cutoff,
    )
    patient_ids = list((await db.execute(cand_stmt)).scalars().all())

    pushed = 0
    skipped_recent = 0
    skipped_no_phone = 0
    for pid in patient_ids:
        patient = await db.get(Patient, pid)
        if patient is None or not patient.phone:
            skipped_no_phone += 1
            continue
        if await _recently_pushed(db, phone=patient.phone, cutoff=cutoff):
            skipped_recent += 1
            continue

        obs_stmt = (
            select(MetricObservation)
            .where(
                MetricObservation.patient_id == pid,
                MetricObservation.source == "patient_self_report",
                MetricObservation.observed_at >= cutoff,
            )
            .order_by(MetricObservation.observed_at.desc())
        )
        observations = list((await db.execute(obs_stmt)).scalars().all())
        summary = summarize_observations(observations)
        if not summary:
            continue
        # Idempotent enqueue keyed per patient per day. ``_recently_pushed``
        # above is the primary window guard, but two overlapping sweep runs
        # could both pass it and then double-enqueue (TOCTOU); the idempotency
        # key closes that race at the DB level (ON CONFLICT DO NOTHING).
        created = await scheduled_events_repo.enqueue_idempotent(
            db,
            event_type=WEEKLY_TREND_EVENT_TYPE,
            patient_id=patient.phone,
            payload={"patient_db_id": pid, "summary": summary},
            idempotency_key=(
                f"weekly_trend:{patient.phone}:{when.date().isoformat()}"
            ),
            scheduled_for=when,
        )
        if created is None:
            # Already pushed today (concurrent sweep won the race) — deduped.
            skipped_recent += 1
            continue
        pushed += 1

    log.info(
        "weekly trend sweep: %d candidates, %d pushed, %d recent, %d no-phone",
        len(patient_ids),
        pushed,
        skipped_recent,
        skipped_no_phone,
    )
    return {
        "candidates": len(patient_ids),
        "pushed": pushed,
        "skipped_recent": skipped_recent,
        "skipped_no_phone": skipped_no_phone,
    }

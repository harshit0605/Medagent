"""Adherence-drop pattern alerter.

Doctors look at the daily digest and see today's tickets, but
they don't naturally surface a patient who's been quietly missing
doses all week. The pattern is real and clinically meaningful:
"this patient took 95% of doses in the prior month, last 7 days
they're at 30%" usually signals something — side effects they
didn't report, depression, life event, lost meds, etc.

This sweep runs periodically and opens an ``adherence_drop`` ops
ticket for any patient whose recent adherence rate is below the
threshold AND volume is high enough to be statistical signal.
The ticket lands in the doctor's daily digest "open tickets"
section + the regular ops queue.

Adherence rate definition (matches ``_adherence_summary`` in
main.py):

    rate = taken / (taken + missed + skipped)

``delayed`` events are excluded — they're a partial good (took
the dose, just late) and counting them as missed would be
unfairly punitive. ``scheduled`` (future) events are excluded
trivially because they haven't happened yet.

Hysteresis: open at < threshold (default 60%), auto-resolve at
>= recovery_threshold (default 75%). The 15-pp band prevents
flicker when a patient's rate oscillates around the boundary.

Min volume: at least ``MIN_SCHEDULED`` completed events in the
window. With a 7-day window + 1-dose-per-day regimen, that's 7
— tightening volume below this would let a patient who happened
to miss 3 of 4 doses fire an alert that's noise.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AdherenceEvent, AdherenceStatus, Patient
from app.db.repositories import ops_tickets as ops_tickets_repo

log = logging.getLogger(__name__)


CATEGORY: str = "adherence_drop"
PRIORITY: str = "high"  # clinically meaningful, doctor-relevant
SLA_MINUTES: int = 1440  # 24h — drops aren't emergencies, but should
                          # be reviewed within a day


# Statuses that count toward the denominator. ``delayed`` is
# intentionally excluded — late-but-taken is still adherence.
_COMPLETED_STATUSES: tuple[AdherenceStatus, ...] = (
    AdherenceStatus.taken,
    AdherenceStatus.missed,
    AdherenceStatus.skipped,
)


def _drop_threshold() -> float:
    """Adherence-rate floor below which the alert opens. Default
    60% — well below the typical "good adherence" 80% so we're
    not crying wolf on routine variance."""
    try:
        return float(os.getenv("ADHERENCE_DROP_THRESHOLD", "0.60"))
    except ValueError:
        return 0.60


def _recovery_threshold() -> float:
    """Rate at or above which an open ticket auto-resolves. The
    15-pp gap between this and the open threshold gives us the
    hysteresis band — a patient at 65% won't flicker."""
    try:
        return float(
            os.getenv("ADHERENCE_DROP_RECOVERY_THRESHOLD", "0.75")
        )
    except ValueError:
        return 0.75


def _min_scheduled() -> int:
    """Minimum completed events (taken + missed + skipped) in the
    window before the rate is trusted. With a 7-day window + once-
    daily regimen this is just 7 — the volume floor that prevents
    a patient who missed 3 of 4 doses from firing an alert that's
    statistical noise."""
    try:
        return int(os.getenv("ADHERENCE_DROP_MIN_SCHEDULED", "7"))
    except ValueError:
        return 7


def _window_days() -> int:
    """Rolling-window span for the rate computation. 7 days is
    tight enough to catch a recent drop quickly while wide enough
    that one bad day doesn't dominate."""
    try:
        return int(os.getenv("ADHERENCE_DROP_WINDOW_DAYS", "7"))
    except ValueError:
        return 7


def _compute_rate(
    taken: int, missed: int, skipped: int
) -> tuple[float, int]:
    """Return ``(rate, completed_count)`` matching the
    ``_adherence_summary`` semantics — late-but-taken counts as
    success, ``delayed`` excluded from the denominator entirely."""
    completed = taken + missed + skipped
    if completed == 0:
        return 0.0, 0
    return round(taken / completed, 3), completed


async def sweep_adherence_drops(
    session: AsyncSession, *, now: datetime | None = None
) -> dict[str, Any]:
    """One pass of the adherence-drop sweep. Walks all patients
    with adherence events in the rolling window, computes per-
    patient rates, opens / re-notes / auto-resolves tickets per
    the threshold + hysteresis policy.

    Caller commits — the loop wrapper in
    ``services.scheduler.main`` commits after the call returns.
    """
    when = now or datetime.now(timezone.utc)
    since = when - timedelta(days=_window_days())
    threshold = _drop_threshold()
    recovery = _recovery_threshold()
    min_scheduled = _min_scheduled()

    # Single aggregation query: per-patient per-status counts in
    # the window, joined to patients for the phone key (used as
    # the ops_tickets.patient_id). One round-trip beats N+1.
    stmt = (
        select(
            Patient.id,
            Patient.phone,
            AdherenceEvent.status,
            func.count().label("n"),
        )
        .join(Patient, Patient.id == AdherenceEvent.patient_id)
        .where(AdherenceEvent.scheduled_at >= since)
        .where(
            AdherenceEvent.status.in_(list(_COMPLETED_STATUSES))
        )
        .group_by(Patient.id, Patient.phone, AdherenceEvent.status)
    )
    rows = (await session.execute(stmt)).all()

    # Pivot (patient_id, status, count) tuples into a per-patient
    # dict so the policy walk is a single linear scan.
    per_patient: dict[
        int, dict[str, int]
    ] = {}
    phone_by_id: dict[int, str] = {}
    for patient_id, phone, status, n in rows:
        slot = per_patient.setdefault(
            int(patient_id),
            {"taken": 0, "missed": 0, "skipped": 0},
        )
        slot[status.value] = int(n)
        if phone:
            phone_by_id[int(patient_id)] = phone

    opened: list[int] = []
    auto_resolved: list[int] = []
    re_noted: list[int] = []
    skipped_low_volume: list[int] = []

    for patient_id, counts in per_patient.items():
        phone = phone_by_id.get(patient_id)
        if not phone:
            # No phone → can't key a ticket to this patient.
            # Skip rather than create an orphan ticket.
            continue

        rate, completed = _compute_rate(
            taken=counts["taken"],
            missed=counts["missed"],
            skipped=counts["skipped"],
        )

        existing = await ops_tickets_repo.find_open_for_patient_category(
            session, patient_id=phone, category=CATEGORY
        )

        # Below volume floor: pause the policy. Don't open new
        # tickets but DON'T auto-resolve open ones — a quiet
        # patient (no doses in the window) might just have a
        # paused regimen, not a recovery. The next pass with
        # volume will re-evaluate.
        if completed < min_scheduled:
            skipped_low_volume.append(patient_id)
            continue

        if rate < threshold:
            if existing is None:
                notes = (
                    f"Adherence drop: rate {rate * 100:.1f}% "
                    f"over the last {_window_days()} days "
                    f"({counts['taken']} taken / "
                    f"{counts['missed']} missed / "
                    f"{counts['skipped']} skipped, "
                    f"completed={completed}). "
                    f"Threshold {threshold * 100:.0f}%. "
                    f"Consider a check-in — possible side effects, "
                    f"life events, or supply issues."
                )
                ticket = await ops_tickets_repo.create(
                    session,
                    patient_id=phone,
                    category=CATEGORY,
                    priority=PRIORITY,
                    sla_minutes=SLA_MINUTES,
                    notes=notes,
                )
                opened.append(patient_id)
                log.warning(
                    "adherence drop: opened ticket %s for patient_id=%d "
                    "rate=%.2f%% (taken=%d missed=%d skipped=%d)",
                    ticket.id,
                    patient_id,
                    rate * 100,
                    counts["taken"],
                    counts["missed"],
                    counts["skipped"],
                )
            else:
                note = (
                    f"still depressed: rate {rate * 100:.1f}% "
                    f"({counts['taken']}/{completed} doses "
                    f"taken in last {_window_days()} days)"
                )
                await ops_tickets_repo.append_note(
                    session,
                    existing.id,
                    actor="system",
                    note=note,
                )
                re_noted.append(patient_id)
            continue

        if rate >= recovery and existing is not None:
            await ops_tickets_repo.resolve(
                session,
                existing.id,
                actor="system",
                notes=(
                    f"auto-resolved: adherence recovered to "
                    f"{rate * 100:.1f}% "
                    f"({counts['taken']}/{completed} taken in "
                    f"last {_window_days()} days)"
                ),
            )
            auto_resolved.append(patient_id)
            log.info(
                "adherence drop: auto-resolved ticket %s for "
                "patient_id=%d rate=%.2f%%",
                existing.id,
                patient_id,
                rate * 100,
            )

    return {
        "patients_evaluated": len(per_patient),
        "opened": len(opened),
        "opened_patient_ids": opened,
        "auto_resolved": len(auto_resolved),
        "auto_resolved_patient_ids": auto_resolved,
        "re_noted": len(re_noted),
        "re_noted_patient_ids": re_noted,
        "skipped_low_volume": len(skipped_low_volume),
    }

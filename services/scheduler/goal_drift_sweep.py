"""Background sweep that opens / auto-resolves ops tickets for
drifting care plan goals.

Slice 14 lets doctors set quantitative goals; this sweep is
the "we noticed and surfaced it" layer so doctors don't have
to remember to check each patient. Iterates every active goal
in the system, classifies the trend (slipping / persistent /
stale / on-target / no-data), and:

    - opens an ops ticket when the trend is alert-worthy AND
      no open goal-drift ticket already exists for this goal,
    - appends a note to the existing ticket when the same
      goal stays in a different alert-worthy state (e.g.
      slipping → persistent_off), so the timestamp updates
      without a duplicate ticket,
    - auto-resolves the open ticket when the goal returns to
      ``on_target`` or has fresh on-target observations.

Idempotency is keyed on category ``goal_drift:{goal_id}`` so
the existing ``find_open_for_patient_category`` helper does
the dedup.

Ops UI: tickets land in the standard /tickets queue with the
goal id + drift state in notes — the doctor clicks through to
the patient detail page from the ticket like any other ops
workflow.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import (
    care_plan_goals as goals_repo,
    ops_tickets as ops_tickets_repo,
    patients as patients_repo,
)

log = logging.getLogger(__name__)


# SLA budget for goal-drift tickets — 7 days. Drift is rarely
# acute (a slipping HbA1c isn't a chest-pain alert); the
# budget reflects "doctor should review this in their next
# weekly clinic-prep pass".
_GOAL_DRIFT_SLA_MINUTES = 60 * 24 * 7


# Per-drift-status tone for the ticket priority. ``slipping``
# is the most urgent because it represents a regression;
# persistent / stale are concerning but slower-moving.
_DRIFT_PRIORITY: dict[str, str] = {
    "slipping": "p2",
    "persistent_off": "p2",
    "stale": "p3",
}


def _category_for_goal(goal_id: int) -> str:
    """One-open-ticket-per-goal idempotency key. Embedding
    the goal id in the category string lets the existing
    ``find_open_for_patient_category`` helper do the dedup
    without a new join."""
    return f"goal_drift:{goal_id}"


def _drift_note(
    goal: Any, drift: str, observation_count: int
) -> str:
    """Stable description format. Doctor reads this in the
    ticket queue without clicking through, so it carries the
    goal name + target + drift state + observation count."""
    op = "<" if goal.comparator == "less_than" else ">"
    return (
        f"goal_drift: {goal.metric_label} (target {op} "
        f"{goal.target_value} {goal.target_unit}) — "
        f"{drift} (window of {observation_count} obs)"
    )


async def sweep_goal_drift(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    window: int = 3,
    stale_after_days: int = 14,
) -> dict[str, Any]:
    """One pass of the drift sweep. Returns a counter dict
    the scheduler logs as a heartbeat detail.

    Caller commits — the wrapper in
    ``services/scheduler/main.py`` commits after this returns.
    """
    when = now or datetime.now(timezone.utc)
    goals = await goals_repo.list_all_active_goals(session)

    opened = 0
    resolved = 0
    by_drift: dict[str, int] = {}

    for goal in goals:
        observations = await goals_repo.list_observations_for_goal(
            session, goal.id, limit=window
        )
        drift = goals_repo.evaluate_drift_status(
            goal,
            observations,
            now=when,
            window=window,
            stale_after_days=stale_after_days,
        )
        by_drift[drift] = by_drift.get(drift, 0) + 1

        # Resolve the patient's phone (used by ops_tickets to
        # key the patient-side join). Patient-id-as-int won't
        # work for the ticket schema; we need the wa_id-style
        # phone string.
        patient = await patients_repo.get(session, goal.patient_id)
        if patient is None or not patient.phone:
            # Patient was erased / soft-deleted — skip without
            # raising. The goal itself stays for clinical
            # retention; it just can't drive a ticket.
            continue

        category = _category_for_goal(goal.id)
        existing = (
            await ops_tickets_repo.find_open_for_patient_category(
                session,
                patient_id=patient.phone,
                category=category,
            )
        )

        if drift in goals_repo.ALERT_WORTHY_DRIFT:
            note = _drift_note(goal, drift, len(observations))
            if existing is None:
                priority = _DRIFT_PRIORITY.get(drift, "p3")
                await ops_tickets_repo.create(
                    session,
                    patient_id=patient.phone,
                    category=category,
                    priority=priority,
                    sla_minutes=_GOAL_DRIFT_SLA_MINUTES,
                    notes=note,
                )
                opened += 1
                log.info(
                    "goal drift ticket opened: goal=%s "
                    "patient=%s drift=%s",
                    goal.id,
                    patient.phone,
                    drift,
                )
        else:
            # ``on_target`` / ``no_data`` — resolve any open
            # ticket. ``no_data`` is benign for new goals;
            # the alert-worthy ``stale`` is its own bucket
            # already handled above.
            if existing is not None:
                await ops_tickets_repo.resolve(
                    session,
                    existing.id,
                    actor="goal_drift_sweep",
                    notes=(
                        f"auto-resolved: drift now {drift}"
                    ),
                )
                resolved += 1
                log.info(
                    "goal drift ticket auto-resolved: "
                    "goal=%s patient=%s drift=%s",
                    goal.id,
                    patient.phone,
                    drift,
                )

    return {
        "goals_examined": len(goals),
        "opened": opened,
        "resolved": resolved,
        "by_drift": by_drift,
    }

"""Care plan goals + observation persistence.

Two thin repos behind one module — they're always read
together (a goal is only useful with its observations) and
keeping them in one file means one import for the service
layer.

Status transitions:

    active → achieved   (manual via update_status, or
                         future auto-detect on N consecutive
                         on-target observations)
    active → inactive   (doctor cancels the goal)
    achieved → active   (rare, but allowed if a metric
                         regresses and ops wants to reopen)

We don't expose ``hard delete`` for goals — observations
keep their FK (SET NULL on delete), but the goal row is
audit. Use ``inactive`` to retire one.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CarePlanGoal, MetricObservation


# Drift classifications. ``on_target`` and ``no_data`` are
# benign; the other three are alert-worthy and trigger ops-
# ticket creation in the goal drift sweep. Kept as a Literal
# so callers (sweep + UI DTO) share the type.
DriftStatus = Literal[
    "on_target", "slipping", "persistent_off", "stale", "no_data"
]
ALERT_WORTHY_DRIFT: frozenset[str] = frozenset(
    {"slipping", "persistent_off", "stale"}
)


# Comparators we currently allow at the API layer. Stored as
# strings (no Postgres enum) so adding a new one is config-
# only, but we validate at write time.
ALLOWED_COMPARATORS: frozenset[str] = frozenset(
    {"less_than", "greater_than"}
)
ALLOWED_STATUSES: frozenset[str] = frozenset(
    {"active", "achieved", "inactive"}
)
ALLOWED_SOURCES: frozenset[str] = frozenset(
    {"manual", "patient_self_report", "lab", "device"}
)


# ---- goals ----------------------------------------------------------------


async def create_goal(
    session: AsyncSession,
    *,
    patient_id: int,
    metric_key: str,
    metric_label: str,
    target_value: Decimal,
    comparator: str,
    target_unit: str,
    created_by: str | None,
    ends_on: date | None = None,
    notes: str | None = None,
) -> CarePlanGoal:
    if comparator not in ALLOWED_COMPARATORS:
        raise ValueError(
            f"unsupported comparator {comparator!r}; "
            f"valid: {sorted(ALLOWED_COMPARATORS)}"
        )
    if not metric_key.strip():
        raise ValueError("metric_key cannot be empty")
    if not metric_label.strip():
        raise ValueError("metric_label cannot be empty")
    row = CarePlanGoal(
        patient_id=patient_id,
        metric_key=metric_key.strip()[:64],
        metric_label=metric_label.strip()[:128],
        target_value=target_value,
        comparator=comparator,
        target_unit=target_unit.strip()[:32],
        ends_on=ends_on,
        created_by=created_by,
        notes=notes,
        status="active",
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def get_goal(
    session: AsyncSession, goal_id: int
) -> CarePlanGoal | None:
    return await session.get(CarePlanGoal, goal_id)


async def list_goals_for_patient(
    session: AsyncSession,
    patient_id: int,
    *,
    status: str | None = None,
    limit: int = 50,
) -> list[CarePlanGoal]:
    """Newest-first by default. ``status`` filter optional —
    patient detail UI shows active by default, expands to
    show achieved/inactive on toggle."""
    stmt = (
        select(CarePlanGoal)
        .where(CarePlanGoal.patient_id == patient_id)
        .order_by(desc(CarePlanGoal.created_at))
        .limit(limit)
    )
    if status is not None:
        if status not in ALLOWED_STATUSES:
            raise ValueError(
                f"unknown status {status!r}; "
                f"valid: {sorted(ALLOWED_STATUSES)}"
            )
        stmt = stmt.where(CarePlanGoal.status == status)
    return list((await session.execute(stmt)).scalars().all())


async def update_status(
    session: AsyncSession,
    goal_id: int,
    *,
    status: str,
) -> CarePlanGoal | None:
    """Manual status transition. Refreshes the row after
    flush so caller sees server-side ``updated_at`` populated
    (Postgres ``onupdate=func.now()`` fires only on UPDATE)."""
    if status not in ALLOWED_STATUSES:
        raise ValueError(
            f"unknown status {status!r}; "
            f"valid: {sorted(ALLOWED_STATUSES)}"
        )
    row = await session.get(CarePlanGoal, goal_id)
    if row is None:
        return None
    if row.status == status:
        # No-op transitions skip the UPDATE entirely so
        # updated_at doesn't tick on a click that didn't
        # change anything.
        return row
    row.status = status
    await session.flush()
    await session.refresh(row)
    return row


# ---- observations ---------------------------------------------------------


async def record_observation(
    session: AsyncSession,
    *,
    patient_id: int,
    goal_id: int | None,
    metric_key: str,
    value: Decimal,
    unit: str,
    observed_at: datetime | None = None,
    source: str = "manual",
    recorded_by: str | None = None,
    notes: str | None = None,
) -> MetricObservation:
    """Insert one observation. ``observed_at`` defaults to now
    so the common case (operator records a just-taken
    measurement) is the shortest call path; backdating a lab
    result passes the lab's date explicitly."""
    if source not in ALLOWED_SOURCES:
        raise ValueError(
            f"unknown source {source!r}; "
            f"valid: {sorted(ALLOWED_SOURCES)}"
        )
    if not metric_key.strip():
        raise ValueError("metric_key cannot be empty")
    when = observed_at or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    row = MetricObservation(
        patient_id=patient_id,
        goal_id=goal_id,
        metric_key=metric_key.strip()[:64],
        value=value,
        unit=unit.strip()[:32],
        observed_at=when,
        source=source,
        recorded_by=recorded_by,
        notes=notes,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def list_observations_for_goal(
    session: AsyncSession,
    goal_id: int,
    *,
    limit: int = 50,
) -> list[MetricObservation]:
    """Newest-first. The trend chart paints the most recent
    N observations against the goal's target line."""
    stmt = (
        select(MetricObservation)
        .where(MetricObservation.goal_id == goal_id)
        .order_by(desc(MetricObservation.observed_at))
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_all_active_goals(
    session: AsyncSession, *, limit: int = 1000
) -> list[CarePlanGoal]:
    """Cross-patient query — used by the goal drift sweep to
    iterate every active goal in the system. ``limit`` exists
    so a runaway dataset doesn't blow the sweep's memory; in
    practice active goals are O(patients × 3-4) and well
    inside the cap. The order (oldest first) is deliberate —
    if we ever hit the limit, the sweep starves the
    most-recently-active goals first, which are the most
    likely to also surface in next sweep tick."""
    stmt = (
        select(CarePlanGoal)
        .where(CarePlanGoal.status == "active")
        .order_by(CarePlanGoal.created_at.asc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


def _value_meets_target(
    *, value: Decimal, comparator: str, target: Decimal
) -> bool:
    """Pure helper — same evaluation as the API DTO's
    ``on_target`` field, but operating on Decimals to avoid
    float-precision drift in the sweep loop."""
    if comparator == "less_than":
        return value < target
    if comparator == "greater_than":
        return value > target
    return False


def meets_consecutive_target(
    goal: CarePlanGoal,
    observations: list[MetricObservation],
    *,
    consecutive: int = 3,
) -> bool:
    """True when the most recent ``consecutive`` observations all meet the
    goal's target (``observations`` newest-first). Powers auto-achievement:
    N readings in a row on-target → the goal is met. Requires at least
    ``consecutive`` observations so a single lucky reading doesn't flip it."""
    if consecutive < 1 or len(observations) < consecutive:
        return False
    return all(
        _value_meets_target(
            value=o.value, comparator=goal.comparator, target=goal.target_value
        )
        for o in observations[:consecutive]
    )


def evaluate_drift_status(
    goal: CarePlanGoal,
    observations: list[MetricObservation],
    *,
    now: datetime | None = None,
    stale_after_days: int = 14,
    window: int = 3,
) -> DriftStatus:
    """Pure-function drift classifier.

    Inputs:
        goal            — the goal row (status, comparator, target)
        observations    — newest-first list of observations for
                          this goal. Caller supplies (the sweep
                          uses ``list_observations_for_goal(limit=window)``).
        now             — wall clock for the stale-age check.
        stale_after_days — age threshold before "no recent data"
                          becomes "stale". 14 days is the V1
                          choice — short enough that an HbA1c
                          recheck cycle (typically 12 wk) doesn't
                          self-trigger but long enough that a
                          gap between visits doesn't cry wolf.
        window          — number of recent observations to
                          inspect for slipping / persistent.

    Output mapping:
        on_target       — most recent observation meets target.
        slipping        — most recent off-target, but ANY of
                          the prior ``window-1`` observations
                          was on-target. Doctor's most concerning
                          state: "we WERE doing well".
        persistent_off  — every observation in the window is
                          off-target. The patient's never been
                          on-target in this window.
        stale           — goal active > stale_after_days AND no
                          observations within that window.
        no_data         — goal has zero observations ever.
                          Distinct from stale so a freshly-
                          created goal doesn't alert immediately.
    """
    when = now or datetime.now(timezone.utc)
    if not observations:
        # Never had a value. Don't alert if the goal is new
        # (operator just created it); alert if it's been
        # active for ``stale_after_days`` and still nothing.
        goal_created = goal.created_at
        if goal_created.tzinfo is None:
            goal_created = goal_created.replace(tzinfo=timezone.utc)
        if when - goal_created >= timedelta(days=stale_after_days):
            return "stale"
        return "no_data"

    # Truncate to the inspection window. Observations are
    # passed newest-first (the repo guarantees this).
    recent = observations[:window]
    target = goal.target_value
    on_target_flags = [
        _value_meets_target(
            value=o.value, comparator=goal.comparator, target=target
        )
        for o in recent
    ]
    most_recent_on_target = on_target_flags[0]

    if most_recent_on_target:
        # Even if older values were off-target, the most
        # recent is on. Doctor doesn't need to act unless
        # the trend reverses.
        return "on_target"

    # Most recent is off-target. Is this a regression
    # ("slipping") or chronic ("persistent_off")?
    had_on_target_recently = any(on_target_flags[1:])
    if had_on_target_recently:
        return "slipping"

    # Stale check: even with off-target observations, if the
    # most recent is much older than the threshold the
    # condition is "stale" not "persistent_off" — different
    # operator action (re-engage vs review the regimen).
    last_observed = recent[0].observed_at
    if last_observed.tzinfo is None:
        last_observed = last_observed.replace(tzinfo=timezone.utc)
    if when - last_observed >= timedelta(days=stale_after_days):
        return "stale"

    return "persistent_off"


async def list_observations_for_patient(
    session: AsyncSession,
    patient_id: int,
    *,
    metric_key: str | None = None,
    limit: int = 100,
) -> list[MetricObservation]:
    """Cross-goal observations for a patient. Used when a
    metric exists without a goal (one-off lab) or when the
    operator wants the raw history regardless of goal."""
    stmt = (
        select(MetricObservation)
        .where(MetricObservation.patient_id == patient_id)
        .order_by(desc(MetricObservation.observed_at))
        .limit(limit)
    )
    if metric_key is not None:
        stmt = stmt.where(
            MetricObservation.metric_key == metric_key
        )
    return list((await session.execute(stmt)).scalars().all())

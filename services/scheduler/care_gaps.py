"""Care-gap sweep — HEDIS-style standing-orders for cohort patients.

For each patient with a cohort flag (diabetes / cardiac / fall_risk) we
have a standing care plan: a recurring lab/test that should happen at a
fixed cadence. The sweep finds patients overdue for a standing test and
auto-materialises a ``lab_followup`` row in ``due`` state — from there
the existing lab-followup machinery (scheduler reminders, dispatcher,
inbound action handler, ops tickets for "I need help") takes over.

Plans live in the ``care_plans`` table (editable from the ops console).
On a freshly-migrated DB, a seed row exists for each of the original
V1 plans (``cohort_diabetes`` → HbA1c every 180 days, etc.); a clinician
can add or deactivate plans without a deploy. If the table is empty
(e.g. someone deactivated everything), the sweep is a no-op rather
than falling back to a hard-coded list — explicit-empty is a valid
operational state.

Idempotency: a patient with an OPEN lab_followup whose ``test_name``
matches the standing order is skipped (whether ``due`` or ``booked`` —
both are "in flight", we don't want to double-prompt). A patient whose
last MATCHING completed/reviewed lab is within the cadence window is
also skipped — no over-sweep when the patient just had the test.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CarePlan as CarePlanModel
from app.db.models import FollowupStatus, LabFollowup, Patient
from app.db.repositories import care_plan_exemptions as care_plan_exemptions_repo
from app.db.repositories import care_plans as care_plans_repo
from app.db.repositories import cohort_tags as cohort_tags_repo
from app.db.repositories import lab_followups as lab_followups_repo

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CarePlan:
    """One standing-order rule loaded from the ``care_plans`` table.
    Kept as a small frozen dataclass so the sweep can pass it around
    without dragging the SQLAlchemy ORM session into the inner loop.

    Exactly one of ``cohort_attr`` (legacy boolean column) or
    ``cohort_tag_id`` (clinician-authored tag) is set per plan; the
    sweep branches on which is non-None to enumerate cohort members.
    """

    plan_id: int
    test_name: str
    cadence_days: int
    cohort_attr: str | None = None
    cohort_tag_id: int | None = None
    due_in_days: int = 14


def _from_row(row: CarePlanModel) -> CarePlan:
    return CarePlan(
        plan_id=row.id,
        cohort_attr=row.cohort_attr,
        cohort_tag_id=row.cohort_tag_id,
        test_name=row.test_name,
        cadence_days=row.cadence_days,
        due_in_days=row.due_in_days,
    )


async def load_active_plans(db: AsyncSession) -> list[CarePlan]:
    """Read all active care_plans from the DB. Used by the sweep + the
    dashboard count helper so a single source of truth defines what
    counts as a 'gap'."""
    rows = await care_plans_repo.list_active(db)
    return [_from_row(r) for r in rows]


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _patients_in_cohort(
    db: AsyncSession, plan: CarePlan
) -> list[Patient]:
    """Resolve cohort membership for a plan. Two paths:

    - ``plan.cohort_tag_id`` set: patients with that tag in the M2M
    - ``plan.cohort_attr`` set: patients with the boolean column True

    Plans must have exactly one of the two — the orchestrator endpoints
    enforce that on write, so a violation here means corrupt data and
    we surface it as a ValueError (the caller's per-runner try/rollback
    isolates the failure to one plan)."""
    if plan.cohort_tag_id is not None:
        return await cohort_tags_repo.patients_for_tag(db, plan.cohort_tag_id)
    if plan.cohort_attr is None:
        raise ValueError(
            f"care_plan {plan.plan_id} has neither cohort_attr nor cohort_tag_id"
        )
    column = getattr(Patient, plan.cohort_attr, None)
    if column is None:
        raise ValueError(
            f"care_plan {plan.plan_id}: unknown cohort attribute: {plan.cohort_attr}"
        )
    stmt = select(Patient).where(column.is_(True)).order_by(Patient.id)
    return list((await db.execute(stmt)).scalars().all())


async def _has_open_followup(
    db: AsyncSession, *, patient_id: int, test_name: str
) -> bool:
    """True if the patient already has a ``due`` or ``booked`` followup
    matching this test. Match is case-insensitive on test_name."""
    stmt = select(LabFollowup.id).where(
        LabFollowup.patient_id == patient_id,
        LabFollowup.status.in_(
            [FollowupStatus.due, FollowupStatus.booked]
        ),
        LabFollowup.test_name.ilike(test_name),
    )
    return (await db.execute(stmt)).first() is not None


async def _last_completed_at(
    db: AsyncSession, *, patient_id: int, test_name: str
) -> datetime | None:
    """When was this test most recently completed/reviewed for this patient?
    Returns None if never. Used so we don't re-prompt within the cadence."""
    stmt = (
        select(LabFollowup.completed_at, LabFollowup.reviewed_at)
        .where(
            LabFollowup.patient_id == patient_id,
            LabFollowup.test_name.ilike(test_name),
            LabFollowup.status.in_(
                [FollowupStatus.completed, FollowupStatus.reviewed]
            ),
        )
        .order_by(desc(LabFollowup.completed_at))
        .limit(1)
    )
    row = (await db.execute(stmt)).first()
    if row is None:
        return None
    completed_at, reviewed_at = row
    # Prefer completed_at; fall back to reviewed_at when the lab was
    # reviewed without an explicit completion timestamp.
    when = completed_at or reviewed_at
    return _ensure_utc(when) if when is not None else None


async def _sweep_one_plan(
    db: AsyncSession, plan: CarePlan, *, now: datetime
) -> dict[str, int]:
    """Run a single standing-order sweep. Returns counts so the caller
    can roll up across plans."""
    patients = await _patients_in_cohort(db, plan)
    cadence = timedelta(days=plan.cadence_days)
    due_by = (now + timedelta(days=plan.due_in_days)).date()

    materialized = 0
    skipped_open = 0
    skipped_recent = 0
    skipped_exempt = 0

    for patient in patients:
        # Exemption check FIRST — clinical opt-outs override every other
        # gate. We don't even check open_followup, since an exempted
        # patient shouldn't be in the followup pipeline at all.
        exempted_plan_ids = (
            await care_plan_exemptions_repo.active_plan_ids_for_patient(
                db, patient.id, now=now
            )
        )
        if plan.plan_id in exempted_plan_ids:
            skipped_exempt += 1
            continue
        if await _has_open_followup(
            db, patient_id=patient.id, test_name=plan.test_name
        ):
            skipped_open += 1
            continue
        last_done = await _last_completed_at(
            db, patient_id=patient.id, test_name=plan.test_name
        )
        if last_done is not None and (now - last_done) < cadence:
            skipped_recent += 1
            continue
        cohort_label = (
            plan.cohort_attr
            if plan.cohort_attr is not None
            else f"tag#{plan.cohort_tag_id}"
        )
        await lab_followups_repo.create(
            db,
            patient_id=patient.id,
            test_name=plan.test_name,
            due_by=due_by,
            notes=(
                f"Standing care plan ({cohort_label}): test recommended "
                f"every {plan.cadence_days} days."
            ),
        )
        materialized += 1
    if materialized:
        await db.flush()
    return {
        "cohort_size": len(patients),
        "materialized": materialized,
        "skipped_open_followup": skipped_open,
        "skipped_recent_completion": skipped_recent,
        "skipped_exempt": skipped_exempt,
    }


async def sweep_care_gaps(
    db: AsyncSession, *, now: datetime | None = None
) -> dict[str, dict[str, int]]:
    """One full pass across all active care plans. Returns a nested
    dict keyed by ``test_name`` so callers and logs can see which rule
    contributed what. An empty result dict means no active plans exist —
    a valid (if unusual) operational state."""
    when_now = _ensure_utc(now or datetime.now(timezone.utc))
    plans = await load_active_plans(db)
    out: dict[str, dict[str, int]] = {}
    for plan in plans:
        out[plan.test_name] = await _sweep_one_plan(db, plan, now=when_now)
    return out


async def overdue_care_gap_count(db: AsyncSession) -> int:
    """Sum of (cohort patients without open or recent followup) across
    all active plans, EXCLUDING patients currently exempted from the
    matching plan. Cheap enough for a dashboard tile — counts directly
    without materialising. Same gates as the sweep so the tile and the
    sweep agree on what counts as a 'gap'."""
    now = datetime.now(timezone.utc)
    plans = await load_active_plans(db)
    total = 0
    for plan in plans:
        cadence = timedelta(days=plan.cadence_days)
        patients = await _patients_in_cohort(db, plan)
        for patient in patients:
            exempted_plan_ids = (
                await care_plan_exemptions_repo.active_plan_ids_for_patient(
                    db, patient.id, now=now
                )
            )
            if plan.plan_id in exempted_plan_ids:
                continue
            if await _has_open_followup(
                db, patient_id=patient.id, test_name=plan.test_name
            ):
                continue
            last_done = await _last_completed_at(
                db, patient_id=patient.id, test_name=plan.test_name
            )
            if last_done is not None and (now - last_done) < cadence:
                continue
            total += 1
    return total

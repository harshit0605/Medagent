from __future__ import annotations

from sqlalchemy import asc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CarePlan


# Allowlist of cohort attributes a care_plan can target. Mirrors the
# boolean columns on Patient. Adding a new cohort means adding a
# patients column AND a row here. Kept in the repo (not the model) so
# the orchestrator endpoints can import it for Pydantic validation
# without pulling in scheduler internals.
KNOWN_COHORT_ATTRS: tuple[str, ...] = (
    "cohort_diabetes",
    "cohort_cardiac",
    "cohort_fall_risk",
)


async def list_active(session: AsyncSession) -> list[CarePlan]:
    stmt = (
        select(CarePlan)
        .where(CarePlan.active.is_(True))
        .order_by(asc(CarePlan.cohort_attr), asc(CarePlan.test_name))
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_all(session: AsyncSession) -> list[CarePlan]:
    """Active + inactive — used by the ops console editor so deactivated
    plans are still visible (and can be re-activated)."""
    stmt = select(CarePlan).order_by(
        asc(CarePlan.cohort_attr), asc(CarePlan.test_name)
    )
    return list((await session.execute(stmt)).scalars().all())


async def get(session: AsyncSession, plan_id: int) -> CarePlan | None:
    return await session.get(CarePlan, plan_id)


async def find_by_cohort_test(
    session: AsyncSession,
    *,
    test_name: str,
    cohort_attr: str | None = None,
    cohort_tag_id: int | None = None,
) -> CarePlan | None:
    """Lookup by (cohort dimension, test_name). Pass exactly one of
    cohort_attr or cohort_tag_id — caller decides which dimension."""
    if (cohort_attr is None) == (cohort_tag_id is None):
        raise ValueError(
            "find_by_cohort_test requires exactly one of "
            "cohort_attr or cohort_tag_id"
        )
    stmt = select(CarePlan).where(CarePlan.test_name == test_name).limit(1)
    if cohort_attr is not None:
        stmt = stmt.where(CarePlan.cohort_attr == cohort_attr)
    else:
        stmt = stmt.where(CarePlan.cohort_tag_id == cohort_tag_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def create(
    session: AsyncSession,
    *,
    test_name: str,
    cadence_days: int,
    cohort_attr: str | None = None,
    cohort_tag_id: int | None = None,
    due_in_days: int = 14,
    notes: str | None = None,
    created_by: str | None = None,
) -> CarePlan:
    """Plan must reference EXACTLY ONE cohort dimension — the
    orchestrator endpoint validates this; we re-check here to keep
    the constraint visible at the lowest layer."""
    if (cohort_attr is None) == (cohort_tag_id is None):
        raise ValueError(
            "create requires exactly one of cohort_attr or cohort_tag_id"
        )
    row = CarePlan(
        cohort_attr=cohort_attr,
        cohort_tag_id=cohort_tag_id,
        test_name=test_name,
        cadence_days=cadence_days,
        due_in_days=due_in_days,
        active=True,
        notes=notes,
        created_by=created_by,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def update(
    session: AsyncSession,
    plan_id: int,
    *,
    cadence_days: int | None = None,
    due_in_days: int | None = None,
    active: bool | None = None,
    notes: str | None = None,
) -> CarePlan | None:
    """Partial update. ``cohort_attr`` and ``test_name`` are NOT mutable
    here — those define the rule's identity (uq_care_plans_cohort_test);
    a doctor changing the cohort or test should deactivate the old plan
    and create a new one so prior sweeps remain attributable."""
    row = await session.get(CarePlan, plan_id)
    if row is None:
        return None
    if cadence_days is not None:
        row.cadence_days = cadence_days
    if due_in_days is not None:
        row.due_in_days = due_in_days
    if active is not None:
        row.active = active
    if notes is not None:
        row.notes = notes
    await session.flush()
    await session.refresh(row)
    return row


async def deactivate(
    session: AsyncSession, plan_id: int
) -> CarePlan | None:
    return await update(session, plan_id, active=False)

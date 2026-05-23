from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import and_, desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CarePlan, CarePlanExemption


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _active_predicate(now: datetime):
    """SQL predicate matching active exemptions: not revoked AND not
    expired. Reused in list_active and find_active_by_patient_plan."""
    return and_(
        CarePlanExemption.revoked_at.is_(None),
        or_(
            CarePlanExemption.expires_at.is_(None),
            CarePlanExemption.expires_at > now,
        ),
    )


async def find_active_by_patient_plan(
    session: AsyncSession,
    *,
    patient_id: int,
    care_plan_id: int,
    now: datetime | None = None,
) -> CarePlanExemption | None:
    """Single active exemption for a patient + plan, if any."""
    when = _ensure_utc(now or datetime.now(timezone.utc))
    stmt = (
        select(CarePlanExemption)
        .where(CarePlanExemption.patient_id == patient_id)
        .where(CarePlanExemption.care_plan_id == care_plan_id)
        .where(_active_predicate(when))
        .order_by(desc(CarePlanExemption.created_at))
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def active_plan_ids_for_patient(
    session: AsyncSession,
    patient_id: int,
    *,
    now: datetime | None = None,
) -> set[int]:
    """All care_plan_ids the patient is currently exempted from.
    Returned as a set so the sweep can do an O(1) membership check
    inside the per-patient loop."""
    when = _ensure_utc(now or datetime.now(timezone.utc))
    stmt = (
        select(CarePlanExemption.care_plan_id)
        .where(CarePlanExemption.patient_id == patient_id)
        .where(_active_predicate(when))
    )
    rows = (await session.execute(stmt)).scalars().all()
    return set(rows)


async def list_for_patient(
    session: AsyncSession,
    patient_id: int,
    *,
    include_inactive: bool = False,
) -> list[CarePlanExemption]:
    """All exemptions for a patient — used by the patient-detail UI.
    By default returns only currently-active rows; pass
    ``include_inactive=True`` for the full audit trail."""
    stmt = (
        select(CarePlanExemption)
        .where(CarePlanExemption.patient_id == patient_id)
        .order_by(desc(CarePlanExemption.created_at))
    )
    if not include_inactive:
        stmt = stmt.where(_active_predicate(datetime.now(timezone.utc)))
    return list((await session.execute(stmt)).scalars().all())


async def list_for_plan(
    session: AsyncSession, care_plan_id: int
) -> list[CarePlanExemption]:
    """Active exemptions for a specific plan — used by the care-plan
    editor to show 'N patients currently exempted'."""
    stmt = (
        select(CarePlanExemption)
        .where(CarePlanExemption.care_plan_id == care_plan_id)
        .where(_active_predicate(datetime.now(timezone.utc)))
        .order_by(desc(CarePlanExemption.created_at))
    )
    return list((await session.execute(stmt)).scalars().all())


async def get(
    session: AsyncSession, exemption_id: int
) -> CarePlanExemption | None:
    return await session.get(CarePlanExemption, exemption_id)


async def create(
    session: AsyncSession,
    *,
    patient_id: int,
    care_plan_id: int,
    reason: str,
    expires_at: datetime | None = None,
    created_by: str | None = None,
) -> CarePlanExemption:
    """Create a new active exemption. Caller is expected to check for
    an existing active row first (via ``find_active_by_patient_plan``)
    and decide whether to skip / revoke-and-recreate; the repo doesn't
    enforce that itself so the orchestrator endpoint can return a clear
    409 instead of letting a duplicate slip in.

    The ``care_plan_id`` should reference an active care plan — the
    endpoint validates that; we don't replicate the check here so this
    function stays mechanically simple."""
    row = CarePlanExemption(
        patient_id=patient_id,
        care_plan_id=care_plan_id,
        reason=reason,
        expires_at=_ensure_utc(expires_at) if expires_at else None,
        created_by=created_by,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def revoke(
    session: AsyncSession,
    exemption_id: int,
    *,
    revoked_by: str | None = None,
    at: datetime | None = None,
) -> CarePlanExemption | None:
    row = await session.get(CarePlanExemption, exemption_id)
    if row is None:
        return None
    if row.revoked_at is not None:
        # Already revoked — return as-is so the endpoint stays idempotent.
        return row
    row.revoked_at = _ensure_utc(at or datetime.now(timezone.utc))
    row.revoked_by = revoked_by
    await session.flush()
    await session.refresh(row)
    return row


async def list_with_plan_info(
    session: AsyncSession,
    patient_id: int,
    *,
    include_inactive: bool = False,
) -> list[tuple[CarePlanExemption, CarePlan]]:
    """Same as ``list_for_patient`` but joins the CarePlan so the UI
    can render the plan's cohort + test_name without N+1 queries."""
    stmt = (
        select(CarePlanExemption, CarePlan)
        .join(CarePlan, CarePlanExemption.care_plan_id == CarePlan.id)
        .where(CarePlanExemption.patient_id == patient_id)
        .order_by(desc(CarePlanExemption.created_at))
    )
    if not include_inactive:
        stmt = stmt.where(_active_predicate(datetime.now(timezone.utc)))
    rows = (await session.execute(stmt)).all()
    return [(exemption, plan) for exemption, plan in rows]

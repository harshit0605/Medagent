"""Pregnancy episode persistence.

Thin repo behind the pregnancy timeline engine. A pregnancy is a bounded
episode (active → ended); the milestone materializer walks ``list_active``
exactly like the lab-followup materializer walks open follow-ups.

The DB enforces at most one ``active`` row per patient via a partial unique
index (migration 20260510_0038); ``create`` defends the same invariant at the
application layer with a friendly error so callers don't see a raw
IntegrityError.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Pregnancy, PregnancyStatus

ACTIVE = PregnancyStatus.active.value
ENDED = PregnancyStatus.ended.value


async def get(session: AsyncSession, pregnancy_id: int) -> Pregnancy | None:
    return await session.get(Pregnancy, pregnancy_id)


async def get_active_for_patient(
    session: AsyncSession, patient_id: int
) -> Pregnancy | None:
    """The patient's current active pregnancy, or ``None``."""
    stmt = (
        select(Pregnancy)
        .where(Pregnancy.patient_id == patient_id)
        .where(Pregnancy.status == ACTIVE)
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def list_active(session: AsyncSession) -> list[Pregnancy]:
    """All active pregnancies (sweep entry point)."""
    stmt = select(Pregnancy).where(Pregnancy.status == ACTIVE)
    return list((await session.execute(stmt)).scalars().all())


async def create(
    session: AsyncSession,
    *,
    patient_id: int,
    lmp_date: date | None = None,
    edd: date | None = None,
    notes: str | None = None,
) -> Pregnancy:
    """Open a new active pregnancy. Requires at least one of ``lmp_date`` /
    ``edd`` (the engine derives the other). Raises ``ValueError`` if the
    patient already has an active pregnancy."""
    if lmp_date is None and edd is None:
        raise ValueError("at least one of lmp_date or edd is required")
    existing = await get_active_for_patient(session, patient_id)
    if existing is not None:
        raise ValueError(
            f"patient {patient_id} already has an active pregnancy "
            f"(id={existing.id}); end it before opening a new one"
        )
    row = Pregnancy(
        patient_id=patient_id,
        lmp_date=lmp_date,
        edd=edd,
        status=ACTIVE,
        notes=notes,
    )
    session.add(row)
    await session.flush()
    return row


async def end_pregnancy(
    session: AsyncSession,
    pregnancy_id: int,
    *,
    reason: str | None = None,
    at: datetime | None = None,
    birth_outcome: str | None = None,
    delivery_date: date | None = None,
) -> Pregnancy | None:
    """Close a pregnancy episode (delivered / miscarried / corrected). The
    milestone sweep ignores ended rows, so this stops further pregnancy
    reminders.

    When ``birth_outcome == 'delivered'`` and ``delivery_date`` is provided,
    the row's postpartum lifecycle stays untouched here — call
    :func:`start_postpartum` (typically right after, from the same endpoint)
    to flip ``postpartum_active = True`` and begin the PP cadence. Keeping
    the two steps separate lets the caller decide (e.g. an operator who
    closes a pregnancy as ``delivered`` may still want a manual review
    before kicking off PP nudges).
    """
    row = await session.get(Pregnancy, pregnancy_id)
    if row is None:
        return None
    row.status = ENDED
    row.ended_at = at or datetime.now(timezone.utc)
    if reason:
        row.ended_reason = reason[:255]
    if birth_outcome is not None:
        row.birth_outcome = birth_outcome[:32]
    if delivery_date is not None:
        row.delivery_date = delivery_date
    await session.flush()
    return row


# ---- postpartum lifecycle --------------------------------------------------


async def get_postpartum_active_for_patient(
    session: AsyncSession, patient_id: int
) -> Pregnancy | None:
    """The patient's current active postpartum episode (the previously-ended
    pregnancy now in its PP phase), or ``None``. The partial unique index
    ``uq_pregnancies_patient_postpartum_active`` guarantees at most one."""
    stmt = (
        select(Pregnancy)
        .where(Pregnancy.patient_id == patient_id)
        .where(Pregnancy.postpartum_active.is_(True))
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def list_postpartum_active(session: AsyncSession) -> list[Pregnancy]:
    """All postpartum-active pregnancies (PP-sweep entry point)."""
    stmt = select(Pregnancy).where(Pregnancy.postpartum_active.is_(True))
    return list((await session.execute(stmt)).scalars().all())


async def start_postpartum(
    session: AsyncSession,
    pregnancy_id: int,
    *,
    delivery_date: date,
) -> Pregnancy | None:
    """Flip an ended pregnancy into its postpartum phase.

    Requires that the pregnancy is already ``ENDED`` and that
    ``birth_outcome == 'delivered'`` (set via :func:`end_pregnancy`).
    Idempotent: re-calling on a row that's already PP-active is a no-op.
    Raises ``ValueError`` if the patient already has a different PP-active
    row (the partial unique index would also catch this, but we want a
    friendly app-level error).
    """
    row = await session.get(Pregnancy, pregnancy_id)
    if row is None:
        return None
    if row.postpartum_active:
        return row
    if row.status != ENDED:
        raise ValueError(
            f"pregnancy {pregnancy_id} is not ended (status={row.status}); "
            "call end_pregnancy first"
        )
    if row.birth_outcome != "delivered":
        raise ValueError(
            f"pregnancy {pregnancy_id} birth_outcome is "
            f"{row.birth_outcome!r}; postpartum cadence only applies to "
            "'delivered'"
        )
    existing_pp = await get_postpartum_active_for_patient(
        session, row.patient_id
    )
    if existing_pp is not None and existing_pp.id != pregnancy_id:
        raise ValueError(
            f"patient {row.patient_id} already has an active postpartum "
            f"episode (id={existing_pp.id}); end it before starting another"
        )
    row.delivery_date = delivery_date
    row.postpartum_active = True
    await session.flush()
    return row


async def end_postpartum(
    session: AsyncSession,
    pregnancy_id: int,
    *,
    reason: str | None = None,
    at: datetime | None = None,
) -> Pregnancy | None:
    """Close a postpartum episode. The PP sweep ignores rows with
    ``postpartum_active = False``, so this stops further PP reminders.
    Idempotent: re-calling on an already-closed row leaves the original
    ``postpartum_ended_at`` timestamp untouched (the FIRST close is the
    legally-relevant moment)."""
    row = await session.get(Pregnancy, pregnancy_id)
    if row is None:
        return None
    if not row.postpartum_active and row.postpartum_ended_at is not None:
        # Already closed — preserve original timestamp.
        return row
    row.postpartum_active = False
    row.postpartum_ended_at = at or datetime.now(timezone.utc)
    if reason:
        row.postpartum_ended_reason = reason[:255]
    await session.flush()
    return row

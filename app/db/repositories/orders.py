"""Order (refill execute) persistence.

Thin repo behind the partner / refill execute layer. An order links a patient
(and usually a regimen) to a fulfillment partner; the pharmacy adapter fills
in ``partner`` + deep-link / partner_order_id. Status + substitution helpers
keep the lifecycle transitions in one place with value validation.
"""

from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order, OrderStatus

VALID_STATUSES: frozenset[str] = frozenset(s.value for s in OrderStatus)
# Orders still in flight — used to dedupe a re-tap on "Reorder".
OPEN_STATUSES: frozenset[str] = frozenset(
    {
        OrderStatus.pending.value,
        OrderStatus.processing.value,
        OrderStatus.shipped.value,
    }
)
VALID_SUBSTITUTION_STATUSES: frozenset[str] = frozenset(
    {"proposed", "approved", "declined"}
)


async def get(session: AsyncSession, order_id: int) -> Order | None:
    return await session.get(Order, order_id)


async def list_for_patient(
    session: AsyncSession, patient_id: int, *, limit: int = 50
) -> list[Order]:
    stmt = (
        select(Order)
        .where(Order.patient_id == patient_id)
        .order_by(desc(Order.created_at))
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_open_for_regimen(
    session: AsyncSession, regimen_id: int
) -> Order | None:
    """The newest still-in-flight order for a regimen, or ``None``. Used to
    avoid creating a duplicate when a patient taps Reorder twice."""
    stmt = (
        select(Order)
        .where(Order.regimen_id == regimen_id)
        .where(Order.status.in_(OPEN_STATUSES))
        .order_by(desc(Order.created_at))
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def create(
    session: AsyncSession,
    *,
    patient_id: int,
    medication_name: str,
    dose: str | None = None,
    quantity: str | None = None,
    regimen_id: int | None = None,
    partner: str = "manual",
    partner_order_id: str | None = None,
    partner_deeplink: str | None = None,
    status: str = OrderStatus.pending.value,
    requested_via: str | None = None,
    notes: str | None = None,
) -> Order:
    if status not in VALID_STATUSES:
        raise ValueError(
            f"unknown order status {status!r}; valid: {sorted(VALID_STATUSES)}"
        )
    row = Order(
        patient_id=patient_id,
        regimen_id=regimen_id,
        medication_name=medication_name,
        dose=dose,
        quantity=quantity,
        partner=partner,
        partner_order_id=partner_order_id,
        partner_deeplink=partner_deeplink,
        status=status,
        requested_via=requested_via,
        notes=notes,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def set_status(
    session: AsyncSession, order_id: int, *, status: str
) -> Order | None:
    if status not in VALID_STATUSES:
        raise ValueError(
            f"unknown order status {status!r}; valid: {sorted(VALID_STATUSES)}"
        )
    row = await session.get(Order, order_id)
    if row is None:
        return None
    row.status = status
    await session.flush()
    await session.refresh(row)
    return row


async def propose_substitution(
    session: AsyncSession,
    order_id: int,
    *,
    medication: str,
    note: str | None = None,
) -> Order | None:
    """Record a partner-proposed substitution pending patient approval."""
    row = await session.get(Order, order_id)
    if row is None:
        return None
    row.substitution_status = "proposed"
    row.substitution_medication = medication[:255]
    if note:
        row.substitution_note = note
    await session.flush()
    await session.refresh(row)
    return row


async def resolve_substitution(
    session: AsyncSession, order_id: int, *, approved: bool
) -> Order | None:
    """Apply the patient's approve/decline decision to a proposed substitution."""
    row = await session.get(Order, order_id)
    if row is None:
        return None
    row.substitution_status = "approved" if approved else "declined"
    await session.flush()
    await session.refresh(row)
    return row

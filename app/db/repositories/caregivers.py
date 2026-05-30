from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import asc, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Caregiver


CONSENT_PENDING = "pending"
CONSENT_CONFIRMED = "confirmed"
CONSENT_DECLINED = "declined"
CONSENT_REVOKED = "revoked"

KNOWN_CONSENT_STATUSES: tuple[str, ...] = (
    CONSENT_PENDING,
    CONSENT_CONFIRMED,
    CONSENT_DECLINED,
    CONSENT_REVOKED,
)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def list_for_patient(
    session: AsyncSession,
    patient_id: int,
    *,
    include_inactive: bool = False,
) -> list[Caregiver]:
    stmt = (
        select(Caregiver)
        .where(Caregiver.patient_id == patient_id)
        .order_by(asc(Caregiver.full_name))
    )
    if not include_inactive:
        stmt = stmt.where(Caregiver.active.is_(True))
    return list((await session.execute(stmt)).scalars().all())


async def list_active_recap_recipients(
    session: AsyncSession, patient_id: int
) -> list[Caregiver]:
    """Active + confirmed-consent + notify_on_recap caregivers. The
    recap-send fan-out walks this list. Single SQL query (no N+1)
    so the recap endpoint stays cheap even with many caregivers."""
    stmt = (
        select(Caregiver)
        .where(Caregiver.patient_id == patient_id)
        .where(Caregiver.active.is_(True))
        .where(Caregiver.consent_status == CONSENT_CONFIRMED)
        .where(Caregiver.notify_on_recap.is_(True))
        .order_by(asc(Caregiver.full_name))
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_active_dose_recipients(
    session: AsyncSession, patient_id: int
) -> list[Caregiver]:
    """Active + confirmed-consent + ``notify_on_dose_reminder`` caregivers.

    The dose-reminder dispatcher fan-out walks this list when the global
    ``CAREGIVER_DOSE_FANOUT_ENABLED`` flag is on. Mirror of
    :func:`list_active_recap_recipients`."""
    stmt = (
        select(Caregiver)
        .where(Caregiver.patient_id == patient_id)
        .where(Caregiver.active.is_(True))
        .where(Caregiver.consent_status == CONSENT_CONFIRMED)
        .where(Caregiver.notify_on_dose_reminder.is_(True))
        .order_by(asc(Caregiver.full_name))
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_active_confirmed(
    session: AsyncSession, patient_id: int
) -> list[Caregiver]:
    """All active + confirmed-consent caregivers for a patient, regardless of
    per-channel notify flags. Used for HIGH-signal alerts (e.g. a missed-dose
    streak on a cardiac patient — SoT §3B) where any consented caregiver should
    be told, not just those opted into routine dose/recap fan-out."""
    stmt = (
        select(Caregiver)
        .where(Caregiver.patient_id == patient_id)
        .where(Caregiver.active.is_(True))
        .where(Caregiver.consent_status == CONSENT_CONFIRMED)
        .order_by(asc(Caregiver.full_name))
    )
    return list((await session.execute(stmt)).scalars().all())


async def find_active_confirmed_by_phone(
    session: AsyncSession,
    *,
    phone: str,
    patient_id: int | None = None,
) -> Caregiver | None:
    """Look up an active + confirmed-consent caregiver row by phone, optionally
    scoped to a single patient.

    Used by the inbound dose-action handler: when a button-tap arrives from
    a phone that doesn't match any ``patients.phone``, we consult this to
    see if the sender is a known caregiver acting on behalf of a patient.
    The patient-id scope makes the call O(1) when we know who the action
    targets (the adherence event already names the patient)."""
    stmt = (
        select(Caregiver)
        .where(Caregiver.phone == phone)
        .where(Caregiver.active.is_(True))
        .where(Caregiver.consent_status == CONSENT_CONFIRMED)
    )
    if patient_id is not None:
        stmt = stmt.where(Caregiver.patient_id == patient_id)
    stmt = stmt.order_by(desc(Caregiver.created_at)).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def get(
    session: AsyncSession, caregiver_id: int
) -> Caregiver | None:
    return await session.get(Caregiver, caregiver_id)


async def find_by_phone(
    session: AsyncSession, *, patient_id: int, phone: str
) -> Caregiver | None:
    """Used by the inbound consent-reply handler — when the caregiver's
    phone messages us with YES/NO, we look up the pending row by
    (patient, phone) so we can flip consent_status."""
    stmt = (
        select(Caregiver)
        .where(Caregiver.patient_id == patient_id)
        .where(Caregiver.phone == phone)
        .order_by(desc(Caregiver.created_at))
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def create(
    session: AsyncSession,
    *,
    patient_id: int,
    full_name: str,
    phone: str,
    relationship_to_patient: str | None = None,
    notify_on_recap: bool = True,
) -> Caregiver:
    row = Caregiver(
        patient_id=patient_id,
        full_name=full_name.strip(),
        phone=phone.strip(),
        relationship_to_patient=(relationship_to_patient or "").strip() or None,
        consent_status=CONSENT_PENDING,
        notify_on_recap=notify_on_recap,
        active=True,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def confirm_consent(
    session: AsyncSession,
    caregiver_id: int,
    *,
    confirmed_by: str,
    at: datetime | None = None,
) -> Caregiver | None:
    """Mark a pending caregiver as confirmed — either via a verbal
    consent recorded by ops, or via an inbound YES from the caregiver's
    phone. Idempotent: confirming an already-confirmed row leaves the
    original confirmation timestamp + actor in place."""
    row = await session.get(Caregiver, caregiver_id)
    if row is None:
        return None
    if row.consent_status == CONSENT_CONFIRMED:
        return row
    row.consent_status = CONSENT_CONFIRMED
    row.consent_confirmed_at = _ensure_utc(at or datetime.now(timezone.utc))
    row.consent_confirmed_by = confirmed_by
    await session.flush()
    await session.refresh(row)
    return row


async def decline_consent(
    session: AsyncSession,
    caregiver_id: int,
    *,
    at: datetime | None = None,
) -> Caregiver | None:
    """Caregiver explicitly opted out — terminal for this row."""
    row = await session.get(Caregiver, caregiver_id)
    if row is None:
        return None
    row.consent_status = CONSENT_DECLINED
    await session.flush()
    await session.refresh(row)
    return row


async def revoke_consent(
    session: AsyncSession,
    caregiver_id: int,
    *,
    at: datetime | None = None,
) -> Caregiver | None:
    """Caregiver previously confirmed, now wants to stop. Distinct from
    declined so the audit log preserves the original confirmation."""
    row = await session.get(Caregiver, caregiver_id)
    if row is None:
        return None
    row.consent_status = CONSENT_REVOKED
    await session.flush()
    await session.refresh(row)
    return row


async def update(
    session: AsyncSession,
    caregiver_id: int,
    *,
    full_name: str | None = None,
    relationship_to_patient: str | None = None,
    notify_on_recap: bool | None = None,
    active: bool | None = None,
) -> Caregiver | None:
    """Partial update. Phone is intentionally NOT mutable — it's the
    identity for the inbound-reply lookup. To change the phone, deactivate
    + create a new row so consent has to be re-established."""
    row = await session.get(Caregiver, caregiver_id)
    if row is None:
        return None
    if full_name is not None:
        row.full_name = full_name.strip()
    if relationship_to_patient is not None:
        row.relationship_to_patient = (
            relationship_to_patient.strip() or None
        )
    if notify_on_recap is not None:
        row.notify_on_recap = notify_on_recap
    if active is not None:
        row.active = active
    await session.flush()
    await session.refresh(row)
    return row

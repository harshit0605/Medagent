from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AppointmentRecap, RecapStatus


async def create_draft(
    session: AsyncSession,
    *,
    appointment_id: int,
    patient_id: int,
    doctor_id: int,
    doctor_notes: str | None,
    structured_payload: dict,
    authored_by: str | None = None,
) -> AppointmentRecap:
    row = AppointmentRecap(
        appointment_id=appointment_id,
        patient_id=patient_id,
        doctor_id=doctor_id,
        doctor_notes=doctor_notes,
        structured_payload=structured_payload or {},
        status=RecapStatus.draft,
        authored_by=authored_by,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def get(session: AsyncSession, recap_id: int) -> AppointmentRecap | None:
    return await session.get(AppointmentRecap, recap_id)


async def get_for_appointment(
    session: AsyncSession, appointment_id: int
) -> AppointmentRecap | None:
    stmt = select(AppointmentRecap).where(
        AppointmentRecap.appointment_id == appointment_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_for_patient(
    session: AsyncSession, patient_id: int, *, limit: int = 50
) -> list[AppointmentRecap]:
    stmt = (
        select(AppointmentRecap)
        .where(AppointmentRecap.patient_id == patient_id)
        .order_by(desc(AppointmentRecap.created_at))
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def update_draft(
    session: AsyncSession,
    recap_id: int,
    *,
    doctor_notes: str | None = None,
    structured_payload: dict | None = None,
    authored_by: str | None = None,
) -> AppointmentRecap | None:
    row = await session.get(AppointmentRecap, recap_id)
    if row is None or row.status != RecapStatus.draft:
        return None
    if doctor_notes is not None:
        row.doctor_notes = doctor_notes
    if structured_payload is not None:
        row.structured_payload = structured_payload
    if authored_by is not None:
        row.authored_by = authored_by
    await session.flush()
    await session.refresh(row)
    return row


async def set_generated_text(
    session: AsyncSession, recap_id: int, *, generated_text: str
) -> AppointmentRecap | None:
    row = await session.get(AppointmentRecap, recap_id)
    if row is None:
        return None
    row.generated_text = generated_text
    await session.flush()
    await session.refresh(row)
    return row


async def mark_sent(
    session: AsyncSession,
    recap_id: int,
    *,
    sent_message_id: str | None,
    generated_text: str,
    sent_at: datetime | None = None,
) -> AppointmentRecap | None:
    row = await session.get(AppointmentRecap, recap_id)
    if row is None:
        return None
    row.status = RecapStatus.sent
    row.generated_text = generated_text
    row.sent_message_id = sent_message_id
    row.sent_at = sent_at or datetime.now(timezone.utc)
    await session.flush()
    await session.refresh(row)
    return row


async def mark_acknowledged(
    session: AsyncSession,
    recap_id: int,
    *,
    at: datetime | None = None,
) -> AppointmentRecap | None:
    row = await session.get(AppointmentRecap, recap_id)
    if row is None:
        return None
    row.status = RecapStatus.acknowledged
    row.acknowledged_at = at or datetime.now(timezone.utc)
    await session.flush()
    await session.refresh(row)
    return row


async def mark_questioned(
    session: AsyncSession, recap_id: int
) -> AppointmentRecap | None:
    row = await session.get(AppointmentRecap, recap_id)
    if row is None:
        return None
    row.status = RecapStatus.questioned
    await session.flush()
    await session.refresh(row)
    return row


async def find_latest_sent_for_patient(
    session: AsyncSession, patient_id: int
) -> AppointmentRecap | None:
    """Used by the inbound handler to figure out which recap a patient's
    'Got it' / 'I have a question' quick-reply refers to. The patient
    most recently received recap is what we ack."""
    stmt = (
        select(AppointmentRecap)
        .where(AppointmentRecap.patient_id == patient_id)
        .where(
            AppointmentRecap.status.in_(
                [RecapStatus.sent, RecapStatus.questioned]
            )
        )
        .order_by(desc(AppointmentRecap.sent_at))
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()

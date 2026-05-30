from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import asc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Appointment, AppointmentStatus


async def create_confirmed(
    session: AsyncSession,
    *,
    patient_id: int,
    doctor_id: int,
    scheduled_for: datetime,
    end_at: datetime,
    calendar_event_id: str | None,
    calendar_html_link: str | None,
    summary: str | None = None,
    source: str = "whatsapp_booking_agent",
    notes: str | None = None,
) -> Appointment:
    if scheduled_for.tzinfo is None:
        scheduled_for = scheduled_for.replace(tzinfo=timezone.utc)
    if end_at.tzinfo is None:
        end_at = end_at.replace(tzinfo=timezone.utc)
    row = Appointment(
        patient_id=patient_id,
        doctor_id=doctor_id,
        scheduled_for=scheduled_for,
        end_at=end_at,
        status=AppointmentStatus.confirmed,
        calendar_event_id=calendar_event_id,
        calendar_html_link=calendar_html_link,
        summary=summary,
        source=source,
        notes=notes,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def list_for_patient(
    session: AsyncSession,
    patient_id: int,
    *,
    upcoming_only: bool = False,
    limit: int = 50,
) -> list[Appointment]:
    stmt = (
        select(Appointment)
        .where(Appointment.patient_id == patient_id)
        .order_by(asc(Appointment.scheduled_for))
        .limit(limit)
    )
    if upcoming_only:
        stmt = stmt.where(Appointment.scheduled_for >= datetime.now(timezone.utc))
    return list((await session.execute(stmt)).scalars().all())


async def get(session: AsyncSession, appointment_id: int) -> Appointment | None:
    return await session.get(Appointment, appointment_id)


async def set_video_link(
    session: AsyncSession, appointment_id: int, *, video_link: str | None
) -> Appointment | None:
    """Set / clear the telehealth video link (I6). The reminder includes a
    'Join here' line when this is set."""
    row = await session.get(Appointment, appointment_id)
    if row is None:
        return None
    row.video_link = (video_link or "").strip() or None
    await session.flush()
    await session.refresh(row)
    return row


async def mark_cancelled(
    session: AsyncSession, appointment_id: int
) -> Appointment | None:
    row = await session.get(Appointment, appointment_id)
    if row is None:
        return None
    row.status = AppointmentStatus.cancelled
    await session.flush()
    await session.refresh(row)
    return row


async def reschedule(
    session: AsyncSession,
    appointment_id: int,
    *,
    new_start: datetime,
    new_end: datetime,
) -> Appointment | None:
    row = await session.get(Appointment, appointment_id)
    if row is None:
        return None
    if new_start.tzinfo is None:
        new_start = new_start.replace(tzinfo=timezone.utc)
    if new_end.tzinfo is None:
        new_end = new_end.replace(tzinfo=timezone.utc)
    row.scheduled_for = new_start
    row.end_at = new_end
    # Status stays "confirmed" — reschedule keeps the booking alive.
    await session.flush()
    await session.refresh(row)
    return row

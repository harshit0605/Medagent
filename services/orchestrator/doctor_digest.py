"""Doctor daily digest — morning panel-management view.

A doctor logging in for the day shouldn't have to click through five
ops-console screens to assemble their day. The digest collapses
everything panel-relevant into a single read-only DTO:

    appointments_today
        The doctor's calendar for today, with patient name, time,
        appointment summary, and a click-through to the patient
        detail. The single most important section — every other
        thing the doctor does today is gated on "did I see the
        right person at the right time?".

    recap_drafts_pending
        Recaps the doctor authored (status=draft) and hasn't yet
        sent. Surfaces the doctor's own outstanding work without
        them having to remember.

    side_effect_reports_24h
        Adverse-event reports filed by the doctor's panel patients
        in the last 24h. Patient-safety signal — these need
        prompt attention; a doctor unaware of an overnight
        reaction is the failure mode this catches.

    open_tickets
        High-priority ops tickets for the doctor's panel that
        aren't yet resolved. Filter to high+critical so a doctor
        with 50 patients doesn't drown in low-priority chatter.

    summary_counts
        Top-line numbers (today_appointments, draft_recaps, etc.)
        for the dashboard header — the doctor scans these in 3
        seconds before drilling in.

Panel definition: patients the doctor has had any appointment with
in the last ``PANEL_WINDOW_DAYS`` (90 by default). That bounds the
queries against ops_tickets / side-effect reports without dropping
inactive patients who might still need attention.

The "today" boundaries default to UTC. A doctor whose timezone is
Asia/Kolkata can pass ``when`` with a tz-aware datetime to align;
the v1 endpoint accepts an explicit date param so the UI can
compute the doctor's local "today" before calling.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Appointment,
    AppointmentStatus,
    AppointmentRecap,
    OpsTicket,
    OpsTicketStatus,
    Patient,
    RecapStatus,
)

log = logging.getLogger(__name__)


PANEL_WINDOW_DAYS: int = 90
SIDE_EFFECT_LOOKBACK_HOURS: int = 24
TICKET_PRIORITY_FILTER: tuple[str, ...] = ("p0", "p1", "high", "critical")


@dataclass
class AppointmentDigestRow:
    appointment_id: int
    patient_id: int
    patient_full_name: str
    scheduled_for: datetime
    end_at: datetime
    status: str
    summary: str | None
    has_pending_recap: bool


@dataclass
class RecapDraftRow:
    recap_id: int
    appointment_id: int
    patient_id: int
    patient_full_name: str
    appointment_date: datetime
    created_at: datetime


@dataclass
class TicketDigestRow:
    ticket_id: int
    patient_id: int | None
    patient_full_name: str | None
    patient_phone: str
    category: str
    priority: str
    status: str
    sla_minutes: int
    created_at: datetime
    sla_breached_at: datetime | None


@dataclass
class SideEffectDigestRow:
    ticket_id: int
    patient_id: int | None
    patient_full_name: str | None
    created_at: datetime
    status: str
    reported_text: str | None


@dataclass
class DoctorDigest:
    doctor_id: int
    doctor_name: str
    when: datetime
    appointments_today: list[AppointmentDigestRow] = field(
        default_factory=list
    )
    recap_drafts_pending: list[RecapDraftRow] = field(
        default_factory=list
    )
    side_effect_reports_24h: list[SideEffectDigestRow] = field(
        default_factory=list
    )
    open_tickets: list[TicketDigestRow] = field(default_factory=list)
    summary_counts: dict[str, int] = field(default_factory=dict)


# ---- Helpers -------------------------------------------------------------


def _utc_day_bounds(when: datetime) -> tuple[datetime, datetime]:
    """Return [start, end] for the UTC day containing ``when``.
    Used by the appointments-today section. The endpoint accepts
    an explicit date param so a non-UTC doctor can compute their
    local-day window client-side and pass UTC bounds in."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    day_start = datetime.combine(
        when.date(), time.min, tzinfo=when.tzinfo
    )
    day_end = day_start + timedelta(days=1)
    return day_start, day_end


# ---- Section builders ----------------------------------------------------


async def _panel_patient_ids(
    session: AsyncSession,
    doctor_id: int,
    *,
    since: datetime,
) -> list[int]:
    """Distinct patient_ids the doctor has had any appointment with
    since ``since``. Used as the join key for the "open tickets"
    + "side-effect reports" sections so we don't surface tickets
    for patients the doctor has nothing to do with."""
    stmt = (
        select(Appointment.patient_id)
        .where(Appointment.doctor_id == doctor_id)
        .where(Appointment.scheduled_for >= since)
        .distinct()
    )
    return [
        int(pid)
        for pid in (await session.execute(stmt)).scalars().all()
    ]


async def _patient_name_lookup(
    session: AsyncSession, patient_ids: list[int]
) -> dict[int, tuple[str, str]]:
    """Return ``{patient_id: (full_name, phone)}`` for the given
    ids. Single round-trip rather than per-row lookup."""
    if not patient_ids:
        return {}
    stmt = select(Patient.id, Patient.full_name, Patient.phone).where(
        Patient.id.in_(patient_ids)
    )
    rows = (await session.execute(stmt)).all()
    return {
        int(pid): (full_name or "", phone or "")
        for pid, full_name, phone in rows
    }


async def _appointments_today_section(
    session: AsyncSession,
    doctor_id: int,
    *,
    day_start: datetime,
    day_end: datetime,
) -> list[AppointmentDigestRow]:
    stmt = (
        select(Appointment)
        .where(Appointment.doctor_id == doctor_id)
        .where(Appointment.scheduled_for >= day_start)
        .where(Appointment.scheduled_for < day_end)
        .order_by(Appointment.scheduled_for)
    )
    appts = list((await session.execute(stmt)).scalars().all())
    if not appts:
        return []

    # Patient names for click-through.
    patient_names = await _patient_name_lookup(
        session, [a.patient_id for a in appts]
    )

    # Pending-recap flags — for each appointment, is there a recap
    # in draft/sent state that the doctor hasn't yet acted on? One
    # query covering all appointments, then group in Python.
    appt_ids = [a.id for a in appts]
    recap_stmt = select(
        AppointmentRecap.appointment_id, AppointmentRecap.status
    ).where(AppointmentRecap.appointment_id.in_(appt_ids))
    recap_rows = (await session.execute(recap_stmt)).all()
    has_pending: dict[int, bool] = {}
    for appt_id, status in recap_rows:
        has_pending[int(appt_id)] = (
            status == RecapStatus.draft
        )

    return [
        AppointmentDigestRow(
            appointment_id=a.id,
            patient_id=a.patient_id,
            patient_full_name=patient_names.get(
                a.patient_id, ("", "")
            )[0],
            scheduled_for=a.scheduled_for,
            end_at=a.end_at,
            status=a.status.value,
            summary=a.summary,
            has_pending_recap=has_pending.get(a.id, False),
        )
        for a in appts
    ]


async def _recap_drafts_section(
    session: AsyncSession, doctor_id: int
) -> list[RecapDraftRow]:
    """Recap drafts authored by this doctor. Surfaces the
    doctor's own outstanding work — drafts they started but
    haven't yet sent."""
    stmt = (
        select(AppointmentRecap, Appointment)
        .join(
            Appointment,
            Appointment.id == AppointmentRecap.appointment_id,
        )
        .where(AppointmentRecap.doctor_id == doctor_id)
        .where(AppointmentRecap.status == RecapStatus.draft)
        .order_by(desc(AppointmentRecap.created_at))
        .limit(50)
    )
    rows = (await session.execute(stmt)).all()
    if not rows:
        return []
    patient_names = await _patient_name_lookup(
        session, [a.patient_id for _r, a in rows]
    )
    return [
        RecapDraftRow(
            recap_id=r.id,
            appointment_id=a.id,
            patient_id=a.patient_id,
            patient_full_name=patient_names.get(
                a.patient_id, ("", "")
            )[0],
            appointment_date=a.scheduled_for,
            created_at=r.created_at,
        )
        for r, a in rows
    ]


async def _side_effect_section(
    session: AsyncSession,
    *,
    panel_phones: list[str],
    since: datetime,
    name_by_phone: dict[str, tuple[int, str]],
) -> list[SideEffectDigestRow]:
    """``side_effect_report`` tickets opened in the last 24h for
    the doctor's panel. Limit to the panel so doctors with 50
    patients don't drown in unrelated reports."""
    if not panel_phones:
        return []
    stmt = (
        select(OpsTicket)
        .where(OpsTicket.category == "side_effect_report")
        .where(OpsTicket.patient_id.in_(panel_phones))
        .where(OpsTicket.created_at >= since)
        .order_by(desc(OpsTicket.created_at))
        .limit(50)
    )
    tickets = list((await session.execute(stmt)).scalars().all())
    if not tickets:
        return []

    # Lazy import; the extractor lives in side_effect_handler (next to
    # the writer that produces the notes format).
    from services.orchestrator.side_effect_handler import (
        _extract_reported_text,
    )

    out: list[SideEffectDigestRow] = []
    for t in tickets:
        pid_name = name_by_phone.get(t.patient_id, (None, None))
        out.append(
            SideEffectDigestRow(
                ticket_id=t.id,
                patient_id=pid_name[0],
                patient_full_name=pid_name[1],
                created_at=t.created_at,
                status=t.status.value,
                reported_text=_extract_reported_text(t.notes),
            )
        )
    return out


async def _open_tickets_section(
    session: AsyncSession,
    *,
    panel_phones: list[str],
    name_by_phone: dict[str, tuple[int, str]],
) -> list[TicketDigestRow]:
    """High/critical ops tickets for the doctor's panel that
    aren't yet resolved. Excludes side-effect reports (they have
    their own section) and infrastructure-level tickets like
    ``platform:delivery`` (those are ops, not doctor)."""
    if not panel_phones:
        return []
    stmt = (
        select(OpsTicket)
        .where(OpsTicket.patient_id.in_(panel_phones))
        .where(
            OpsTicket.status.in_(
                [OpsTicketStatus.open, OpsTicketStatus.acknowledged]
            )
        )
        .where(OpsTicket.priority.in_(TICKET_PRIORITY_FILTER))
        .where(OpsTicket.category != "side_effect_report")
        .order_by(desc(OpsTicket.created_at))
        .limit(50)
    )
    tickets = list((await session.execute(stmt)).scalars().all())
    if not tickets:
        return []

    out: list[TicketDigestRow] = []
    for t in tickets:
        pid_name = name_by_phone.get(t.patient_id, (None, None))
        out.append(
            TicketDigestRow(
                ticket_id=t.id,
                patient_id=pid_name[0],
                patient_full_name=pid_name[1],
                patient_phone=t.patient_id,
                category=t.category,
                priority=t.priority,
                status=t.status.value,
                sla_minutes=t.sla_minutes,
                created_at=t.created_at,
                sla_breached_at=t.sla_breached_at,
            )
        )
    return out


# ---- Public entrypoint ---------------------------------------------------


async def build_digest(
    session: AsyncSession,
    *,
    doctor_id: int,
    doctor_name: str,
    when: datetime | None = None,
    panel_window_days: int = PANEL_WINDOW_DAYS,
) -> DoctorDigest:
    """Aggregate the doctor's daily digest. Returns a populated
    ``DoctorDigest`` regardless of whether each section has data —
    empty sections render as "nothing here today" in the UI rather
    than missing fields."""
    when = when or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    day_start, day_end = _utc_day_bounds(when)
    panel_since = when - timedelta(days=panel_window_days)
    side_effect_since = when - timedelta(
        hours=SIDE_EFFECT_LOOKBACK_HOURS
    )

    # Build panel + lookups in parallel-friendly order. The panel
    # query is cheap (indexed on doctor_scheduled), the name
    # lookup batches the panel into a single round-trip.
    panel_ids = await _panel_patient_ids(
        session, doctor_id, since=panel_since
    )
    panel_names = await _patient_name_lookup(session, panel_ids)
    panel_phones = [phone for _name, phone in panel_names.values() if phone]
    name_by_phone: dict[str, tuple[int, str]] = {
        phone: (pid, name)
        for pid, (name, phone) in panel_names.items()
        if phone
    }

    appointments_today = await _appointments_today_section(
        session, doctor_id, day_start=day_start, day_end=day_end
    )
    recap_drafts = await _recap_drafts_section(session, doctor_id)
    side_effects = await _side_effect_section(
        session,
        panel_phones=panel_phones,
        since=side_effect_since,
        name_by_phone=name_by_phone,
    )
    open_tickets = await _open_tickets_section(
        session,
        panel_phones=panel_phones,
        name_by_phone=name_by_phone,
    )

    # Top-line counters for the digest header. Useful for the
    # 3-second skim — each value clicks through to the section.
    summary_counts = {
        "appointments_today": len(appointments_today),
        "appointments_with_pending_recap": sum(
            1 for a in appointments_today if a.has_pending_recap
        ),
        "appointments_today_confirmed": sum(
            1
            for a in appointments_today
            if a.status == AppointmentStatus.confirmed.value
        ),
        "recap_drafts_pending": len(recap_drafts),
        "side_effect_reports_24h": len(side_effects),
        "open_tickets": len(open_tickets),
        "panel_size": len(panel_ids),
    }

    return DoctorDigest(
        doctor_id=doctor_id,
        doctor_name=doctor_name,
        when=when,
        appointments_today=appointments_today,
        recap_drafts_pending=recap_drafts,
        side_effect_reports_24h=side_effects,
        open_tickets=open_tickets,
        summary_counts=summary_counts,
    )

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import and_, desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OpsTicket, OpsTicketStatus


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_note_entry(actor: str, note: str, when: datetime) -> str:
    """Append-friendly note format. Real activity-log table is overkill for
    V1 — we just prepend timestamped lines onto the existing free-form
    notes field so it grows into a chronological log."""
    ts = _ensure_utc(when).strftime("%Y-%m-%d %H:%M UTC")
    return f"[{ts}] {actor}: {note}"


def _append_note(existing: str | None, actor: str, note: str | None) -> str | None:
    """Prepend a timestamped entry to ``existing`` notes. Returns the
    existing notes unchanged when ``note`` is empty."""
    if not note:
        return existing
    line = _format_note_entry(actor, note, datetime.now(timezone.utc))
    if existing:
        return f"{line}\n{existing}"
    return line


async def create(
    session: AsyncSession,
    *,
    patient_id: str,
    category: str,
    priority: str,
    sla_minutes: int,
    notes: str | None = None,
) -> OpsTicket:
    row = OpsTicket(
        patient_id=patient_id,
        category=category,
        priority=priority,
        sla_minutes=sla_minutes,
        status=OpsTicketStatus.open,
        notes=notes,
    )
    session.add(row)
    await session.flush()
    return row


async def get(session: AsyncSession, ticket_id: int) -> OpsTicket | None:
    return await session.get(OpsTicket, ticket_id)


async def list_tickets(session: AsyncSession, *, status: str | None = None) -> list[OpsTicket]:
    stmt = select(OpsTicket).order_by(desc(OpsTicket.created_at))
    if status is not None:
        stmt = stmt.where(OpsTicket.status == OpsTicketStatus(status))
    return list((await session.execute(stmt)).scalars().all())


async def open_counts_by_patient(
    session: AsyncSession,
) -> dict[str, int]:
    """Return ``{patient_id: open_ticket_count}`` in a single grouped query.

    The patients-list page needs an open-ticket count per row; doing it with
    a per-patient fetch (or, worse, re-fetching ALL open tickets inside the
    loop) is an N+1 / full-scan storm. One GROUP BY serves the whole page.
    Keyed by ``patient_id`` (the phone string the ticket carries)."""
    stmt = (
        select(OpsTicket.patient_id, func.count())
        .where(OpsTicket.status == OpsTicketStatus.open)
        .group_by(OpsTicket.patient_id)
    )
    rows = (await session.execute(stmt)).all()
    return {pid: count for pid, count in rows}


async def list_with_filters(
    session: AsyncSession,
    *,
    status: str | None = None,
    category: str | None = None,
    assigned_to: str | None = None,
    include_snoozed: bool = True,
    only_snoozed: bool = False,
    only_active: bool = False,
    limit: int = 200,
) -> list[OpsTicket]:
    """Flexible list query for the ops queue page.

    ``include_snoozed=False``: drop tickets whose ``snoozed_until`` is in
    the future (they're temporarily out of the active queue).
    ``only_snoozed=True``: invert — only show tickets with future
    ``snoozed_until``.
    ``only_active=True``: status in {open, acknowledged} AND not snoozed.
    """
    stmt = select(OpsTicket).order_by(desc(OpsTicket.created_at)).limit(limit)
    now = datetime.now(timezone.utc)

    if status is not None:
        stmt = stmt.where(OpsTicket.status == OpsTicketStatus(status))
    if category is not None:
        stmt = stmt.where(OpsTicket.category == category)
    if assigned_to is not None:
        if assigned_to == "":
            stmt = stmt.where(OpsTicket.assigned_to.is_(None))
        else:
            stmt = stmt.where(OpsTicket.assigned_to == assigned_to)
    if only_active:
        stmt = stmt.where(
            OpsTicket.status.in_(
                [OpsTicketStatus.open, OpsTicketStatus.acknowledged]
            )
        )
        stmt = stmt.where(
            or_(
                OpsTicket.snoozed_until.is_(None),
                OpsTicket.snoozed_until <= now,
            )
        )
    elif only_snoozed:
        stmt = stmt.where(
            and_(
                OpsTicket.snoozed_until.isnot(None),
                OpsTicket.snoozed_until > now,
            )
        )
    elif not include_snoozed:
        stmt = stmt.where(
            or_(
                OpsTicket.snoozed_until.is_(None),
                OpsTicket.snoozed_until <= now,
            )
        )
    return list((await session.execute(stmt)).scalars().all())


async def list_for_patient(
    session: AsyncSession, patient_id: str, *, limit: int = 50
) -> list[OpsTicket]:
    stmt = (
        select(OpsTicket)
        .where(OpsTicket.patient_id == patient_id)
        .order_by(desc(OpsTicket.created_at))
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_for_patient_by_category(
    session: AsyncSession,
    patient_id: str,
    category: str,
    *,
    limit: int = 20,
) -> list[OpsTicket]:
    """Patient-scoped tickets filtered by category, newest first.

    Used by the patient-detail page to render category-specific
    timelines (side-effect reports, refill_help requests, etc.) so
    a clinician can see patterns across multiple events. The
    generic ``list_for_patient`` returns ALL ticket categories
    interleaved, which is too noisy for a category-specific view."""
    stmt = (
        select(OpsTicket)
        .where(OpsTicket.patient_id == patient_id)
        .where(OpsTicket.category == category)
        .order_by(desc(OpsTicket.created_at))
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def assign(
    session: AsyncSession,
    ticket_id: int,
    *,
    assigned_to: str | None,
    actor: str,
    note: str | None = None,
) -> OpsTicket | None:
    ticket = await session.get(OpsTicket, ticket_id)
    if ticket is None:
        return None
    ticket.assigned_to = assigned_to or None
    audit_note = (
        f"assigned to {assigned_to}" if assigned_to else "unassigned"
    )
    if note:
        audit_note = f"{audit_note} — {note}"
    ticket.notes = _append_note(ticket.notes, actor, audit_note)
    await session.flush()
    return ticket


async def snooze(
    session: AsyncSession,
    ticket_id: int,
    *,
    until: datetime,
    actor: str,
    note: str | None = None,
) -> OpsTicket | None:
    """Hide the ticket from the active queue until ``until``. Underlying
    status stays unchanged — when the timestamp passes the ticket flows
    back into the active queue automatically."""
    ticket = await session.get(OpsTicket, ticket_id)
    if ticket is None:
        return None
    ticket.snoozed_until = _ensure_utc(until)
    audit = f"snoozed until {ticket.snoozed_until.isoformat(timespec='minutes')}"
    if note:
        audit = f"{audit} — {note}"
    ticket.notes = _append_note(ticket.notes, actor, audit)
    await session.flush()
    return ticket


async def unsnooze(
    session: AsyncSession,
    ticket_id: int,
    *,
    actor: str,
    note: str | None = None,
) -> OpsTicket | None:
    ticket = await session.get(OpsTicket, ticket_id)
    if ticket is None:
        return None
    ticket.snoozed_until = None
    audit_note = "snooze cleared"
    if note:
        audit_note = f"{audit_note} — {note}"
    ticket.notes = _append_note(ticket.notes, actor, audit_note)
    await session.flush()
    return ticket


async def reopen(
    session: AsyncSession,
    ticket_id: int,
    *,
    actor: str,
    note: str | None = None,
) -> OpsTicket | None:
    """Move a resolved/acknowledged ticket back to ``open``. Clears
    resolved_at + acknowledged_at + sla_breached_at so the SLA window
    re-starts from now and a re-opened ticket isn't pre-stamped as
    breached based on the prior cycle."""
    ticket = await session.get(OpsTicket, ticket_id)
    if ticket is None:
        return None
    ticket.status = OpsTicketStatus.open
    ticket.resolved_at = None
    ticket.acknowledged_at = None
    ticket.snoozed_until = None
    ticket.sla_breached_at = None
    ticket.notes = _append_note(ticket.notes, actor, "reopened" + (f" — {note}" if note else ""))
    await session.flush()
    return ticket


async def append_note(
    session: AsyncSession,
    ticket_id: int,
    *,
    actor: str,
    note: str,
) -> OpsTicket | None:
    ticket = await session.get(OpsTicket, ticket_id)
    if ticket is None:
        return None
    ticket.notes = _append_note(ticket.notes, actor, note)
    await session.flush()
    return ticket


async def find_open_for_patient_category(
    session: AsyncSession, *, patient_id: str, category: str
) -> OpsTicket | None:
    """Used by automated escalators (missed-dose sweep, etc.) to avoid
    creating a second open ticket while the first is still pending. Open
    means status in {open, acknowledged} — only `resolved` clears the slot."""
    stmt = (
        select(OpsTicket)
        .where(OpsTicket.patient_id == patient_id)
        .where(OpsTicket.category == category)
        .where(
            OpsTicket.status.in_(
                [OpsTicketStatus.open, OpsTicketStatus.acknowledged]
            )
        )
        .order_by(desc(OpsTicket.created_at))
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_open_for_category(
    session: AsyncSession, *, category: str
) -> list[OpsTicket]:
    """All open/acknowledged tickets in a category. Used by reconciling
    sweeps (e.g. delivery_reconcile) that auto-resolve a whole category's
    worth of tickets once their underlying condition clears."""
    stmt = (
        select(OpsTicket)
        .where(OpsTicket.category == category)
        .where(
            OpsTicket.status.in_(
                [OpsTicketStatus.open, OpsTicketStatus.acknowledged]
            )
        )
        .order_by(desc(OpsTicket.created_at))
    )
    return list((await session.execute(stmt)).scalars().all())


async def acknowledge(
    session: AsyncSession,
    ticket_id: int,
    *,
    at: datetime | None = None,
    actor: str = "ops",
    notes: str | None = None,
) -> OpsTicket | None:
    ticket = await session.get(OpsTicket, ticket_id)
    if ticket is None:
        return None
    ticket.status = OpsTicketStatus.acknowledged
    ticket.acknowledged_at = at or datetime.now(timezone.utc)
    audit = "acknowledged" + (f" — {notes}" if notes else "")
    ticket.notes = _append_note(ticket.notes, actor, audit)
    await session.flush()
    return ticket


async def resolve(
    session: AsyncSession,
    ticket_id: int,
    *,
    at: datetime | None = None,
    actor: str = "ops",
    notes: str | None = None,
) -> OpsTicket | None:
    ticket = await session.get(OpsTicket, ticket_id)
    if ticket is None:
        return None
    ticket.status = OpsTicketStatus.resolved
    ticket.resolved_at = at or datetime.now(timezone.utc)
    audit = "resolved" + (f" — {notes}" if notes else "")
    ticket.notes = _append_note(ticket.notes, actor, audit)
    await session.flush()
    return ticket


async def find_breach_candidates(
    session: AsyncSession, *, now: datetime | None = None
) -> list[OpsTicket]:
    """Return tickets that have crossed their SLA threshold but haven't
    been marked as breached yet.

    Filters:
        - status in {open, acknowledged}            (resolved tickets stop the clock)
        - sla_breached_at IS NULL                   (one breach mark per cycle)
        - snoozed_until IS NULL OR <= now           (snoozed → out of active queue, clock paused)
        - now - created_at > sla_minutes            (the actual breach predicate)

    The ``snoozed_until <= now`` branch matters because a ticket that
    was snoozed but is now back in the active queue should be eligible
    for breach marking based on its raw age — once we put it back in
    the queue we expect ops to handle it within the original SLA.
    """
    when = _ensure_utc(now or datetime.now(timezone.utc))
    # Postgres-portable: compute SLA expiry per row as
    # ``created_at + sla_minutes * interval '1 minute'`` and compare
    # to ``now``. Using ``func.make_interval`` keeps the operation
    # server-side and avoids fetching every active ticket.
    sla_due = OpsTicket.created_at + func.make_interval(
        0, 0, 0, 0, 0, OpsTicket.sla_minutes
    )
    stmt = (
        select(OpsTicket)
        .where(
            OpsTicket.status.in_(
                [OpsTicketStatus.open, OpsTicketStatus.acknowledged]
            )
        )
        .where(OpsTicket.sla_breached_at.is_(None))
        .where(
            or_(
                OpsTicket.snoozed_until.is_(None),
                OpsTicket.snoozed_until <= when,
            )
        )
        .where(sla_due < when)
        .order_by(OpsTicket.created_at)
    )
    return list((await session.execute(stmt)).scalars().all())


async def mark_sla_breached(
    session: AsyncSession,
    ticket_id: int,
    *,
    when: datetime | None = None,
) -> OpsTicket | None:
    """Stamp ``sla_breached_at = when`` (default now) and append a
    ``[SLA breached]`` audit note. Idempotent: if the column is
    already set we leave it untouched and return the row unchanged.

    Caller is responsible for committing — the breach sweep batches
    multiple marks per cycle."""
    ticket = await session.get(OpsTicket, ticket_id)
    if ticket is None:
        return None
    if ticket.sla_breached_at is not None:
        return ticket  # idempotent — already marked
    stamp = _ensure_utc(when or datetime.now(timezone.utc))
    ticket.sla_breached_at = stamp
    age_minutes = int(
        (stamp - _ensure_utc(ticket.created_at)).total_seconds() // 60
    )
    audit = (
        f"SLA breached — sla_minutes={ticket.sla_minutes}, "
        f"age_minutes={age_minutes}"
    )
    ticket.notes = _append_note(ticket.notes, "system", audit)
    await session.flush()
    return ticket


async def snapshot(session: AsyncSession) -> dict[str, int]:
    rows = (
        await session.execute(
            select(OpsTicket.status, func.count()).group_by(OpsTicket.status)
        )
    ).all()
    counts = {status.value: 0 for status in OpsTicketStatus}
    for status, count in rows:
        counts[status.value] = count
    counts["total"] = sum(counts.values())
    return counts

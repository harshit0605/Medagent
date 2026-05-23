"""Inbound calendar sync — Google Calendar → our appointments.

The booking agent + appointment endpoints push appointment
changes OUT to Google Calendar (create / patch / delete). But
doctors also edit their calendars directly: cancelling a slot
because of an emergency, dragging an appointment to a different
time, etc. Without inbound sync, our ``appointments`` row goes
stale and patients keep getting reminders for events that no
longer exist on the doctor's actual calendar.

This sweep polls Google's incremental ``events.list`` API for
each connected doctor and reconciles the changes against our
appointments. Reconciliation rules:

    Calendar event cancelled (status="cancelled")
        Find our appointment with matching ``calendar_event_id``;
        if it isn't already cancelled, mark it cancelled.

    Calendar event start/end changed
        Find our appointment with matching ``calendar_event_id``;
        if scheduled_for or end_at differ, update them in place.

    Calendar event we don't have
        Skip. The doctor created an event directly in Calendar
        that wasn't booked through us — not our appointment to
        track.

Per-doctor failures are isolated: a single doctor with revoked
OAuth or a sync-token issue doesn't kill the sweep for the rest.

Polling cadence: 10 min default. The lag between a doctor
cancelling in Calendar and our DB reflecting it is bounded by
the cadence + Google's own propagation. Acceptable for medical
bookings; future v2 swaps to webhook push notifications.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Appointment, AppointmentStatus
from app.db.repositories import doctors as doctors_repo
from services.orchestrator import google_calendar as gcal

log = logging.getLogger(__name__)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


async def _reconcile_doctor(
    session: AsyncSession, *, doctor_id: int
) -> dict[str, int]:
    """Pull incremental changes for one doctor + apply them to
    our appointments. Returns a per-doctor counter dict the
    caller aggregates."""
    counters = {
        "changes_received": 0,
        "cancelled": 0,
        "rescheduled": 0,
        "skipped_unknown_event": 0,
        "skipped_already_correct": 0,
    }

    result = await gcal.incremental_sync(session, doctor_id=doctor_id)
    counters["changes_received"] = len(result.changes)

    if not result.changes:
        return counters

    # Look up all appointments matching the changed event IDs in
    # one round-trip rather than N+1.
    event_ids = [c.event_id for c in result.changes]
    stmt = select(Appointment).where(
        Appointment.doctor_id == doctor_id,
        Appointment.calendar_event_id.in_(event_ids),
    )
    appts = list((await session.execute(stmt)).scalars().all())
    appt_by_event_id: dict[str, Appointment] = {
        a.calendar_event_id: a
        for a in appts
        if a.calendar_event_id
    }

    for change in result.changes:
        appt = appt_by_event_id.get(change.event_id)
        if appt is None:
            # Doctor-created event we never booked. Not ours.
            counters["skipped_unknown_event"] += 1
            continue

        if change.cancelled:
            if appt.status == AppointmentStatus.cancelled:
                counters["skipped_already_correct"] += 1
                continue
            appt.status = AppointmentStatus.cancelled
            counters["cancelled"] += 1
            log.warning(
                "calendar sync: doctor %s cancelled appointment %s "
                "(event %s) in Google Calendar — marking cancelled",
                doctor_id,
                appt.id,
                change.event_id,
            )
            continue

        # Live event — check if start/end shifted. Both sides
        # normalised to UTC so naive vs aware doesn't trip the
        # equality check.
        if change.start is None or change.end is None:
            # Defensive — incremental_sync's parser should have
            # filtered these. Skip silently.
            continue
        new_start = _ensure_utc(change.start)
        new_end = _ensure_utc(change.end)
        existing_start = _ensure_utc(appt.scheduled_for)
        existing_end = _ensure_utc(appt.end_at)
        if new_start == existing_start and new_end == existing_end:
            counters["skipped_already_correct"] += 1
            continue
        appt.scheduled_for = new_start
        appt.end_at = new_end
        counters["rescheduled"] += 1
        log.warning(
            "calendar sync: doctor %s rescheduled appointment %s "
            "(event %s) in Google Calendar — %s → %s",
            doctor_id,
            appt.id,
            change.event_id,
            existing_start.isoformat(),
            new_start.isoformat(),
        )

    await session.flush()
    return counters


async def sweep_calendar_changes(
    session: AsyncSession,
) -> dict[str, Any]:
    """One pass of the inbound calendar sync. Iterates every
    doctor with active OAuth, polls Google for changes, and
    reconciles against our ``appointments`` table.

    Returns per-doctor counters + an aggregate. Per-doctor
    failures are caught + logged so one bad doctor (revoked
    token, network error) doesn't kill the sweep for the rest.
    """
    connected = await doctors_repo.list_connected(session)
    if not connected:
        return {
            "doctors_evaluated": 0,
            "totals": {
                "cancelled": 0,
                "rescheduled": 0,
                "skipped_unknown_event": 0,
                "skipped_already_correct": 0,
                "errors": 0,
            },
        }

    totals = {
        "cancelled": 0,
        "rescheduled": 0,
        "skipped_unknown_event": 0,
        "skipped_already_correct": 0,
        "errors": 0,
    }
    per_doctor: list[dict[str, Any]] = []

    for doctor in connected:
        try:
            counters = await _reconcile_doctor(
                session, doctor_id=doctor.id
            )
            for key in (
                "cancelled",
                "rescheduled",
                "skipped_unknown_event",
                "skipped_already_correct",
            ):
                totals[key] += counters.get(key, 0)
            per_doctor.append({"doctor_id": doctor.id, **counters})
        except PermissionError as exc:
            # OAuth revoked — gcal layer already flipped the
            # doctor to ``expired``; just count + move on.
            log.warning(
                "calendar sync: doctor %s OAuth expired (%s)",
                doctor.id,
                exc,
            )
            totals["errors"] += 1
            per_doctor.append(
                {"doctor_id": doctor.id, "error": "oauth_expired"}
            )
        except Exception:  # noqa: BLE001 — keep per-doctor isolation
            log.exception(
                "calendar sync: per-doctor sweep failed for "
                "doctor %s; continuing",
                doctor.id,
            )
            totals["errors"] += 1
            per_doctor.append(
                {"doctor_id": doctor.id, "error": "sweep_failed"}
            )

    return {
        "doctors_evaluated": len(connected),
        "totals": totals,
        "per_doctor": per_doctor,
    }

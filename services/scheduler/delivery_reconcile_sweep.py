"""Per-patient delivery reconciliation sweep — catches *silent patients*.

The platform-wide ``delivery_alert_sweep`` opens ONE ticket when the aggregate
outbound failure rate spikes (a Meta outage, an expired token). But it averages
away the case that matters most clinically: a SINGLE patient whose number went
dead — deactivated WhatsApp, blocked the bot, or the number changed hands.
That patient silently stops receiving dose reminders, refill nudges, and lab
follow-ups while the platform failure rate stays near zero. Nobody notices for
weeks.

This sweep closes that gap. Every ``DELIVERY_RECONCILE_SWEEP_SECONDS`` (default
3600s = 1h) it asks: which recipients have ``>= UNREACHABLE_MIN_FAILURES``
failed deliveries in the last ``UNREACHABLE_WINDOW_DAYS`` AND zero successful
deliveries in that window? For each, it opens an idempotent
``patient_unreachable`` ops ticket so a human can re-establish contact (call the
patient, confirm the number, re-send the opt-in). When a later delivered/read
arrives the ticket auto-resolves.

Idempotency: keyed on ``find_open_for_patient_category(recipient_id,
'patient_unreachable')`` — one open ticket per patient at a time, mirroring the
escalation-dedupe pattern used across the other sweeps. The ``recipient_id`` is
the WhatsApp phone, which is exactly what ``ops_tickets.patient_id`` stores, so
the ticket links to the patient in the ops console.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import (
    ops_tickets as ops_tickets_repo,
    whatsapp_statuses as whatsapp_statuses_repo,
)
from app.logging_redact import redact_phone

log = logging.getLogger(__name__)


CATEGORY: str = "patient_unreachable"
PRIORITY: str = "high"  # a patient missing all their care reminders is serious
SLA_MINUTES: int = 1440  # 24h — not as tight as an outage, but shouldn't linger


def _window_days() -> int:
    try:
        return int(os.getenv("UNREACHABLE_WINDOW_DAYS", "3"))
    except ValueError:
        return 3


def _min_failures() -> int:
    """Failed-delivery count (with zero successes) that flags a patient as
    unreachable. 3 is enough signal that it's not a one-off — a deactivated
    number fails every send."""
    try:
        return int(os.getenv("UNREACHABLE_MIN_FAILURES", "3"))
    except ValueError:
        return 3


async def sweep_unreachable_patients(
    session: AsyncSession, *, now: datetime | None = None
) -> dict[str, Any]:
    """One pass. Opens ``patient_unreachable`` tickets for silent patients and
    auto-resolves ones who are reachable again. Caller commits."""
    when = now or datetime.now(timezone.utc)
    window_start = when - timedelta(days=_window_days())

    unreachable = await whatsapp_statuses_repo.unreachable_recipients(
        session, since=window_start, min_failures=_min_failures()
    )
    unreachable_ids = {u["recipient_id"] for u in unreachable}

    out: dict[str, Any] = {
        "unreachable_count": len(unreachable),
        "opened": 0,
        "auto_resolved": 0,
    }

    # Open / re-note tickets for currently-unreachable patients.
    for u in unreachable:
        recipient = u["recipient_id"]
        existing = await ops_tickets_repo.find_open_for_patient_category(
            session, patient_id=recipient, category=CATEGORY
        )
        err_bits = []
        if u["last_error_code"]:
            err_bits.append(f"code {u['last_error_code']}")
        if u["last_error_title"]:
            err_bits.append(u["last_error_title"])
        err_phrase = (", ".join(err_bits)) or "no error detail captured"
        if existing is None:
            notes = (
                f"Patient appears unreachable on WhatsApp: "
                f"{u['failed']} failed deliveries and zero successful "
                f"deliveries in the last {_window_days()} day(s). "
                f"Last failure: {err_phrase}. Confirm the number, call the "
                f"patient, and re-send the opt-in if needed — they are not "
                f"receiving any care reminders."
            )
            ticket = await ops_tickets_repo.create(
                session,
                patient_id=recipient,
                category=CATEGORY,
                priority=PRIORITY,
                sla_minutes=SLA_MINUTES,
                notes=notes,
            )
            out["opened"] += 1
            log.warning(
                "delivery reconcile: opened unreachable ticket %s for %s "
                "(%d failed, last=%s)",
                ticket.id,
                redact_phone(recipient),
                u["failed"],
                err_phrase,
            )
        # else: ticket already open — leave it; re-noting every hour would
        # spam the timeline with no new signal (the failure count is
        # monotonic until they're reachable again).

    # Auto-resolve open tickets for patients who are now reachable.
    open_tickets = await ops_tickets_repo.list_open_for_category(
        session, category=CATEGORY
    )
    for ticket in open_tickets:
        if ticket.patient_id in unreachable_ids:
            continue  # still unreachable — keep the ticket open
        reachable = await whatsapp_statuses_repo.has_recent_success(
            session, recipient_id=ticket.patient_id, since=window_start
        )
        if reachable:
            await ops_tickets_repo.resolve(
                session,
                ticket.id,
                actor="system",
                notes=(
                    "auto-resolved: patient is reachable again "
                    "(a delivered/read receipt arrived within the window)."
                ),
            )
            out["auto_resolved"] += 1
            log.info(
                "delivery reconcile: auto-resolved unreachable ticket %s",
                ticket.id,
            )

    return out

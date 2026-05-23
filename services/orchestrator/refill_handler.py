"""Deterministic handler for refill-action button taps.

The Next.js webhook rewrites a refill-button tap into a marker-prefixed
text like::

    [refill-action] done regimen_id=4
    [refill-action] snoozed regimen_id=4
    [refill-action] help regimen_id=4

This module recognises the marker, mutates the regimen / scheduled_events,
optionally creates an ops_ticket, and returns a short confirmation reply.
No LLM round-trip — pure CRUD.

Cross-patient safety: the regimen's patient_id is verified against the
inbound's resolved Patient (looked up by phone). If they don't match, we
refuse rather than mutate.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from app.db.repositories import ops_tickets as ops_tickets_repo
from app.db.repositories import patients as patients_repo
from app.db.repositories import regimens as regimens_repo
from app.db.repositories import scheduled_events as scheduled_events_repo
from app.db.session import get_sessionmaker
from services.scheduler import refill_reminders

log = logging.getLogger(__name__)


SNOOZE_DURATION = timedelta(days=1)
# Hard cap on per-cycle snoozes. Beyond this we treat it as a sign the
# patient needs a human nudge and convert the snooze into a refill_help
# ticket. Avoids infinite-snooze loops where supply quietly runs to zero.
SNOOZE_CAP_PER_CYCLE = 3
HELP_TICKET_CATEGORY = "refill_help"
HELP_TICKET_PRIORITY = "p3"
HELP_TICKET_SLA_MINUTES = 1440  # 24h

_REFILL_ACTION_RE = re.compile(
    r"^\s*\[refill-action\]\s+(?P<action>done|snoozed|help)\s+"
    r"regimen_id\s*=\s*(?P<id>\d+)\s*$",
    re.I,
)


def looks_like_refill_action(text: str) -> bool:
    return bool(text and _REFILL_ACTION_RE.match(text))


async def handle_refill_action(
    *,
    patient_phone: str,
    new_user_text: str,
) -> dict[str, Any] | None:
    match = _REFILL_ACTION_RE.match(new_user_text or "")
    if match is None:
        return None
    action = match.group("action").lower()
    regimen_id = int(match.group("id"))

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        regimen = await regimens_repo.get(db, regimen_id)
        if regimen is None:
            return _reply(
                "I couldn't find that regimen. It may have been removed.",
                audit_codes=["refill_action_unknown_regimen"],
            )

        # Cross-patient ownership check.
        patient = await patients_repo.get_by_phone(db, patient_phone)
        if patient is None or regimen.patient_id != patient.id:
            log.warning(
                "refill action refused: phone=%s regimen.patient_id=%s",
                patient_phone,
                regimen.patient_id,
            )
            return _reply(
                "I can only update regimens for your own account. Please "
                "contact support if this looks wrong.",
                audit_codes=["refill_action_cross_patient_refused"],
            )

        med_label = (
            f"{regimen.medication_name} ({regimen.dose})"
            if regimen.dose
            else regimen.medication_name
        )
        now = datetime.now(timezone.utc)
        today = now.date()

        if action == "done":
            # Reset the supply cycle. Cancel pending refill events for the
            # OLD cycle (the new cycle's events will materialize on the
            # next scheduler tick).
            await regimens_repo.reset_supply(db, regimen_id, on=today)
            await refill_reminders.cancel_for_regimen(
                db, regimen_id=regimen_id, reason="refill_done"
            )
            await db.commit()
            days = regimen.supply_days_initial or "—"
            return _reply(
                f"Logged: {med_label} refilled. Next reminder in about "
                f"{days} day(s). ✓",
                audit_codes=["refill_action_done"],
            )

        if action == "snoozed":
            # Hard cap to avoid the patient infinitely deferring while
            # supply runs to zero — at the cap we convert the tap into an
            # ops_help ticket so a human can intervene.
            cycle_key = (
                regimen.supply_started_on.isoformat()
                if regimen.supply_started_on
                else None
            )
            if cycle_key is not None:
                snooze_count = await refill_reminders.count_snoozes_for_cycle(
                    db, regimen_id=regimen_id, cycle_key=cycle_key
                )
                if snooze_count >= SNOOZE_CAP_PER_CYCLE:
                    return await _open_help_ticket_for_snooze_cap(
                        db,
                        patient_phone=patient_phone,
                        med_label=med_label,
                        regimen=regimen,
                        snooze_count=snooze_count,
                    )

            # Re-prompt tomorrow at the same time. Don't touch
            # supply_started_on — the supply is genuinely still running out;
            # we're just deferring the prompt by 24h.
            snoozed_until = now + SNOOZE_DURATION
            await scheduled_events_repo.enqueue(
                db,
                event_type="refill_due",
                patient_id=patient_phone,
                payload={
                    "regimen_id": regimen.id,
                    "patient_db_id": regimen.patient_id,
                    "medication_name": regimen.medication_name,
                    "dose": regimen.dose,
                    "stage": "snooze1d",
                    "days_left": _days_left(regimen, today),
                    "supply_runs_out_iso": (
                        refill_reminders.supply_runs_out(regimen).isoformat()
                        if refill_reminders.supply_runs_out(regimen)
                        else None
                    ),
                    "cycle_key": (
                        regimen.supply_started_on.isoformat()
                        if regimen.supply_started_on
                        else None
                    ),
                },
                scheduled_for=snoozed_until,
            )
            await db.commit()
            return _reply(
                f"Snoozed {med_label} refill reminder — I'll prompt you again "
                f"tomorrow.",
                audit_codes=["refill_action_snoozed"],
            )

        # action == "help"
        existing = await ops_tickets_repo.find_open_for_patient_category(
            db, patient_id=patient_phone, category=HELP_TICKET_CATEGORY
        )
        if existing is not None:
            return _reply(
                "We already have a refill help ticket open for you — someone "
                "from the team will be in touch shortly.",
                audit_codes=["refill_action_help_already_open"],
            )
        notes = (
            f"Patient requested refill help for {med_label} "
            f"(regimen={regimen_id}, patient.id={regimen.patient_id})."
        )
        ticket = await ops_tickets_repo.create(
            db,
            patient_id=patient_phone,
            category=HELP_TICKET_CATEGORY,
            priority=HELP_TICKET_PRIORITY,
            sla_minutes=HELP_TICKET_SLA_MINUTES,
            notes=notes,
        )
        await db.commit()
        log.info(
            "refill help ticket %s created for patient %s",
            ticket.id,
            patient_phone,
        )
        return _reply(
            f"Got it — I've flagged your {med_label} refill for our team. "
            f"Someone will reach out soon.",
            audit_codes=["refill_action_help"],
        )


async def _open_help_ticket_for_snooze_cap(
    db,
    *,
    patient_phone: str,
    med_label: str,
    regimen,
    snooze_count: int,
) -> dict[str, Any]:
    """Convert the snooze tap into a refill_help ops_ticket once the patient
    has hit the per-cycle cap. Idempotent — if there's already an open
    refill_help ticket, just respond with the 'we're on it' message."""
    existing = await ops_tickets_repo.find_open_for_patient_category(
        db, patient_id=patient_phone, category=HELP_TICKET_CATEGORY
    )
    if existing is None:
        notes = (
            f"Patient hit the snooze cap ({snooze_count} snoozes) on their "
            f"{med_label} refill reminder (regimen={regimen.id}, "
            f"patient.id={regimen.patient_id}). Please follow up — supply "
            f"will run out without intervention."
        )
        ticket = await ops_tickets_repo.create(
            db,
            patient_id=patient_phone,
            category=HELP_TICKET_CATEGORY,
            priority=HELP_TICKET_PRIORITY,
            sla_minutes=HELP_TICKET_SLA_MINUTES,
            notes=notes,
        )
        await db.commit()
        log.info(
            "snooze cap hit (count=%s) → opened refill_help ticket %s for %s",
            snooze_count,
            ticket.id,
            patient_phone,
        )
    else:
        log.info(
            "snooze cap hit (count=%s) but ticket %s already open for %s",
            snooze_count,
            existing.id,
            patient_phone,
        )
        await db.commit()
    return _reply(
        f"You've snoozed your {med_label} refill {snooze_count} time(s). "
        f"I've flagged this for our team — someone will reach out so we "
        f"can keep you supplied without the patient running out.",
        audit_codes=["refill_action_snooze_cap_hit"],
    )


def _days_left(regimen, today) -> int | None:
    expiry = refill_reminders.supply_runs_out(regimen)
    if expiry is None:
        return None
    return max(0, (expiry - today).days)


def _reply(
    body: str,
    *,
    audit_codes: list[str],
    buttons: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "response_body": body,
        "audit_reasons": audit_codes,
        "intent": "refill_request",
        "risk_level": "low",
        "escalation_required": False,
        "use_template": False,
        "template_name": None,
        "quick_replies": ["CALL", "HELP"],
        "buttons": buttons or [],
        "list_rows": [],
        "list_button_label": None,
        "list_section_title": None,
    }

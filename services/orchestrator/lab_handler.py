"""Deterministic handler for lab-followup button taps.

The Next.js webhook rewrites a lab-button tap into a marker-prefixed text
like::

    [lab-action] booked lab_followup_id=4
    [lab-action] completed lab_followup_id=4
    [lab-action] help lab_followup_id=4

State transitions::

    due → booked      (Booked tap)
    due/booked → completed   (Completed tap; cancels future reminders)
    any → help        (Need help tap; opens an ops_ticket)

Cross-patient safety: the lab's patient_id is verified against the
inbound's resolved Patient (looked up by phone). Help-ticket creation
is idempotent against an already-open ``lab_help`` ticket.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from app.db.models import FollowupStatus
from app.db.repositories import lab_followups as lab_followups_repo
from app.db.repositories import ops_tickets as ops_tickets_repo
from app.db.repositories import patients as patients_repo
from app.db.session import get_sessionmaker
from services.scheduler import lab_followups as lab_followups_scheduler

log = logging.getLogger(__name__)


HELP_TICKET_CATEGORY = "lab_help"
HELP_TICKET_PRIORITY = "p3"
HELP_TICKET_SLA_MINUTES = 1440  # 24h

_LAB_ACTION_RE = re.compile(
    r"^\s*\[lab-action\]\s+(?P<action>booked|completed|help)\s+"
    r"lab_followup_id\s*=\s*(?P<id>\d+)\s*$",
    re.I,
)


def looks_like_lab_action(text: str) -> bool:
    return bool(text and _LAB_ACTION_RE.match(text))


async def handle_lab_action(
    *,
    patient_phone: str,
    new_user_text: str,
) -> dict[str, Any] | None:
    match = _LAB_ACTION_RE.match(new_user_text or "")
    if match is None:
        return None
    action = match.group("action").lower()
    lab_id = int(match.group("id"))

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        lab = await lab_followups_repo.get(db, lab_id)
        if lab is None:
            return _reply(
                "I couldn't find that lab follow-up. It may have been removed.",
                audit_codes=["lab_action_unknown"],
            )

        patient = await patients_repo.get_by_phone(db, patient_phone)
        if patient is None or lab.patient_id != patient.id:
            log.warning(
                "lab action refused: phone=%s lab.patient_id=%s",
                patient_phone,
                lab.patient_id,
            )
            return _reply(
                "I can only update labs for your own account. Please contact "
                "support if this looks wrong.",
                audit_codes=["lab_action_cross_patient_refused"],
            )

        test = lab.test_name
        now = datetime.now(timezone.utc)

        if action == "booked":
            if lab.status in (
                FollowupStatus.completed,
                FollowupStatus.reviewed,
            ):
                return _reply(
                    f"Your {test} test is already logged as "
                    f"{lab.status.value}. No change made.",
                    audit_codes=[f"lab_action_already_{lab.status.value}"],
                )
            if lab.status == FollowupStatus.booked:
                return _reply(
                    f"Already noted: {test} is booked.",
                    audit_codes=["lab_action_already_booked"],
                )
            await lab_followups_repo.mark_booked(db, lab_id, at=now)
            await db.commit()
            return _reply(
                f"Logged: {test} is booked. I'll remind you the day before "
                f"and after to confirm it was done.",
                audit_codes=["lab_action_booked"],
            )

        if action == "completed":
            if lab.status == FollowupStatus.reviewed:
                return _reply(
                    f"Your {test} test is already reviewed. ✓",
                    audit_codes=["lab_action_already_reviewed"],
                )
            if lab.status == FollowupStatus.completed:
                return _reply(
                    f"Already logged: {test} completed. Your care team will "
                    f"review the results.",
                    audit_codes=["lab_action_already_completed"],
                )
            await lab_followups_repo.mark_completed(db, lab_id, at=now)
            # Cancel future reminders — the patient's done.
            await lab_followups_scheduler.cancel_for_lab_followup(
                db, lab_followup_id=lab_id, reason="lab_completed"
            )
            await db.commit()
            return _reply(
                f"Logged: {test} completed. ✓ Your care team will review the "
                f"results and reach out with next steps.",
                audit_codes=["lab_action_completed"],
            )

        # action == "help"
        existing = await ops_tickets_repo.find_open_for_patient_category(
            db, patient_id=patient_phone, category=HELP_TICKET_CATEGORY
        )
        if existing is not None:
            return _reply(
                "We already have a lab help ticket open for you — someone "
                "from the team will be in touch shortly.",
                audit_codes=["lab_action_help_already_open"],
            )
        notes = (
            f"Patient asked for help with {test} lab follow-up "
            f"(lab_followup={lab_id}, patient.id={lab.patient_id}, "
            f"due_by={lab.due_by}, status={lab.status.value})."
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
            "lab help ticket %s created for patient %s",
            ticket.id,
            patient_phone,
        )
        return _reply(
            f"Got it — I've flagged your {test} lab follow-up for our team. "
            f"Someone will reach out soon.",
            audit_codes=["lab_action_help"],
        )


def _reply(
    body: str,
    *,
    audit_codes: list[str],
    buttons: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "response_body": body,
        "audit_reasons": audit_codes,
        # No dedicated lab intent — surface as general so existing API
        # consumers don't need updates.
        "intent": "general_question",
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

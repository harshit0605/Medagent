"""Deterministic handler for dose-action button taps.

The Next.js webhook rewrites a dose-button tap into a marker-prefixed text
like::

    [dose-action] taken adherence_event_id=42

This module recognises that marker, updates the AdherenceEvent state,
optionally schedules a snooze, and returns a short confirmation reply.
No LLM round-trip — pure CRUD against ``adherence_events`` +
``scheduled_events``.

Cross-patient safety: the AdherenceEvent's patient_id is verified against
the inbound's resolved Patient (looked up by phone). If they don't match,
we refuse rather than mutate someone else's row.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AdherenceStatus
from app.db.repositories import adherence_events as adherence_events_repo
from app.db.repositories import patients as patients_repo
from app.db.repositories import regimens as regimens_repo
from app.db.repositories import scheduled_events as scheduled_events_repo
from app.db.session import get_sessionmaker

log = logging.getLogger(__name__)


SNOOZE_DURATION = timedelta(minutes=30)

# Matches the webhook-rewritten dose-action text — see
# apps/ops_console/src/lib/whatsapp-bot.ts.
_DOSE_ACTION_RE = re.compile(
    r"^\s*\[dose-action\]\s+(?P<action>taken|snoozed|skipped|late_taken)\s+"
    r"adherence_event_id\s*=\s*(?P<id>\d+)\s*$",
    re.I,
)


def looks_like_dose_action(text: str) -> bool:
    """Cheap pre-check the agent_workflow uses to short-circuit before the
    LLM intent + safety nodes run."""
    return bool(text and _DOSE_ACTION_RE.match(text))


async def handle_dose_action(
    *,
    patient_phone: str,
    new_user_text: str,
) -> dict[str, Any] | None:
    """Process a dose button tap. Returns a delta dict for the AgentState
    (response_body + audit + button hints) or ``None`` when the inbound
    doesn't actually look like a dose action — caller should fall back."""
    match = _DOSE_ACTION_RE.match(new_user_text or "")
    if match is None:
        return None
    action = match.group("action").lower()
    adherence_event_id = int(match.group("id"))

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        adherence = await adherence_events_repo.get(db, adherence_event_id)
        if adherence is None:
            return _reply(
                "I couldn't find that dose record. It may have already been "
                "logged or removed.",
                audit_codes=["dose_action_unknown"],
            )

        # Cross-patient ownership check — refuse to mutate a different
        # patient's row even if a malicious / confused tap somehow arrives.
        patient = await patients_repo.get_by_phone(db, patient_phone)
        if patient is None or adherence.patient_id != patient.id:
            log.warning(
                "dose action refused: phone=%s adherence.patient_id=%s",
                patient_phone,
                adherence.patient_id,
            )
            return _reply(
                "I can only update doses for your own account. Please contact "
                "support if this looks wrong.",
                audit_codes=["dose_action_cross_patient_refused"],
            )

        regimen = (
            await regimens_repo.get(db, adherence.regimen_id)
            if adherence.regimen_id
            else None
        )
        med_label = (
            f"{regimen.medication_name} ({regimen.dose})"
            if regimen
            else "your medication"
        )
        now = datetime.now(timezone.utc)

        # ``late_taken`` ALWAYS overrides — the patient did take the dose,
        # they're just confirming after the grace window. Records
        # ``late_confirmed=true`` in metadata so adherence reports can
        # distinguish on-time from after-the-fact confirmations.
        if action == "late_taken":
            if adherence.status == AdherenceStatus.taken:
                return _reply(
                    f"Already logged: {med_label} taken. ✓",
                    audit_codes=["dose_action_late_taken_already_taken"],
                )
            await adherence_events_repo.update_status(
                db,
                adherence_event_id,
                status=AdherenceStatus.taken,
                confirmed_at=now,
                metadata={
                    "late_confirmed": True,
                    "previous_status": adherence.status.value,
                },
            )
            await db.commit()
            return _reply(
                f"Logged late: {med_label} taken. Try to tap Taken closer "
                f"to the dose time so we can keep the timing accurate.",
                audit_codes=["dose_action_late_taken"],
            )

        # Already-handled (missed / skipped / taken / delayed): refuse to
        # overwrite with a fresh Taken/Snoozed/Skipped tap, but offer the
        # patient a Mark-as-taken button as a recovery path. Stresses that
        # on-time tapping matters while still letting them correct the
        # record.
        if adherence.status != AdherenceStatus.scheduled:
            already = adherence.status.value
            if already == "taken":
                body = f"This dose is already logged as taken. ✓"
                buttons: list[dict[str, str]] = []
            else:
                body = (
                    f"This dose is logged as {already}. Try to tap on time "
                    f"so reminders stay accurate. If you actually took it, "
                    f"tap Mark as taken below."
                )
                buttons = [
                    {
                        "id": f"dose_late_taken:{adherence_event_id}",
                        "label": "Mark as taken",
                        "action": "dose_late_taken",
                    },
                ]
            return _reply(
                body,
                audit_codes=[f"dose_action_already_{already}"],
                buttons=buttons,
            )

        if action == "taken":
            await adherence_events_repo.mark_taken(db, adherence_event_id, at=now)
            await db.commit()
            return _reply(
                f"Logged: {med_label} taken. ✓",
                audit_codes=["dose_action_taken"],
            )

        if action == "skipped":
            await adherence_events_repo.mark_skipped(
                db, adherence_event_id, at=now, reason="patient_skipped"
            )
            await db.commit()
            return _reply(
                f"Logged: {med_label} skipped. We'll prompt you at the next "
                f"scheduled dose.",
                audit_codes=["dose_action_skipped"],
            )

        # Snooze: keep the adherence row alive (status=delayed), schedule a
        # new dose_due ScheduledEvent 30 min out pointing at the SAME
        # adherence_event_id so the eventual response still updates the
        # original row (preserves the original scheduled_at for adherence
        # rate calculations).
        snoozed_until = now + SNOOZE_DURATION
        await adherence_events_repo.mark_delayed(
            db, adherence_event_id, snoozed_until=snoozed_until
        )
        await scheduled_events_repo.enqueue(
            db,
            event_type="dose_due",
            patient_id=patient_phone,
            payload={
                "adherence_event_id": adherence_event_id,
                "regimen_id": adherence.regimen_id,
                "patient_db_id": adherence.patient_id,
                "medication_name": regimen.medication_name if regimen else None,
                "dose": regimen.dose if regimen else None,
                "scheduled_at_iso": adherence.scheduled_at.isoformat(),
                "snoozed_from_action_id": adherence_event_id,
            },
            scheduled_for=snoozed_until,
        )
        await db.commit()
        return _reply(
            f"Snoozed {med_label} — I'll remind you again in 30 minutes.",
            audit_codes=["dose_action_snoozed"],
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
        "intent": "adherence_update",
        "risk_level": "low",
        "escalation_required": False,
        "use_template": False,
        "template_name": None,
        "quick_replies": ["CALL", "HELP"],
        "buttons": buttons or [],
        "list_rows": [],
        "list_button_label": None,
        "list_section_title": None,
        # Flow handling: dose actions don't enter / continue the booking
        # flow. Caller should preserve any existing current_flow.
    }

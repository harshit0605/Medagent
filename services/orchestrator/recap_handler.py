"""Deterministic handler for after-visit recap quick-reply responses.

The patient receives a recap WhatsApp message that ends with::

    Reply OK to acknowledge, or QUESTION if anything is unclear.

We accept two equivalent input forms:

1. Marker-prefixed (when the Next.js webhook translates a button-id tap)::

       [recap-action] ack recap_id=12
       [recap-action] question recap_id=12

2. Plain text (when the patient just types). The patient's most recent
   ``sent``/``questioned`` recap is the one we update.

Behavior:

- ``ack``    → mark recap acknowledged.
- ``question`` → mark recap questioned AND open an ops_ticket
  (category ``recap_question``) so a clinician can follow up. Idempotent
  against an already-open ticket on the same recap.

Cross-patient safety: the recap row's ``patient_id`` must match the
inbound's resolved patient (looked up by phone).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.db.repositories import appointment_recaps as appointment_recaps_repo
from app.db.repositories import ops_tickets as ops_tickets_repo
from app.db.repositories import patients as patients_repo
from app.db.session import get_sessionmaker

log = logging.getLogger(__name__)


RECAP_QUESTION_CATEGORY = "recap_question"
RECAP_QUESTION_PRIORITY = "p3"
RECAP_QUESTION_SLA_MINUTES = 1440  # 24h


_MARKER_RE = re.compile(
    r"^\s*\[recap-action\]\s+(?P<action>ack|question)\s+"
    r"recap_id\s*=\s*(?P<id>\d+)\s*$",
    re.I,
)

# Plain-text recognisers. These trigger ONLY when the patient has a
# recent unacknowledged recap; the orchestrator falls through to the
# normal LLM path otherwise (so a generic "OK" doesn't get swallowed).
_ACK_RE = re.compile(
    r"^\s*(?:ok(?:ay)?|got it|thanks|thank you|noted|understood|👍)\s*\.?\s*$",
    re.I,
)
_QUESTION_RE = re.compile(
    r"^\s*(?:question|i\s+have\s+a\s+question|i\s+have\s+question|"
    r"can\s+(?:you|i)\s+(?:please\s+)?clarify|please\s+explain|what\s+do\s+you\s+mean)"
    r"\b.*$",
    re.I,
)


def looks_like_recap_action(text: str) -> bool:
    """Cheap pre-router check: returns True if the text COULD be a recap
    response. The actual handler short-circuits if there's no recent
    recap for the patient — that's the real gate."""
    if not text:
        return False
    if _MARKER_RE.match(text):
        return True
    return bool(_ACK_RE.match(text) or _QUESTION_RE.match(text))


def _parse_action(
    text: str,
) -> tuple[str | None, int | None]:
    """Returns (action, recap_id) or (None, None). Recap id may be None
    for plain-text matches — the handler resolves it from the patient's
    most-recent sent recap."""
    marker = _MARKER_RE.match(text or "")
    if marker:
        return marker.group("action").lower(), int(marker.group("id"))
    if _ACK_RE.match(text or ""):
        return "ack", None
    if _QUESTION_RE.match(text or ""):
        return "question", None
    return None, None


async def handle_recap_action(
    *, patient_phone: str, new_user_text: str
) -> dict[str, Any] | None:
    """Process a recap quick-reply. Returns None when the inbound isn't
    a recap response (e.g. plain-text "OK" but no recent recap)."""
    action, recap_id_hint = _parse_action(new_user_text)
    if action is None:
        return None

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        patient = await patients_repo.get_by_phone(db, patient_phone)
        if patient is None:
            return None

        if recap_id_hint is not None:
            recap = await appointment_recaps_repo.get(db, recap_id_hint)
        else:
            recap = await appointment_recaps_repo.find_latest_sent_for_patient(
                db, patient.id
            )

        # Plain-text "OK" with no pending recap — fall through to the
        # normal LLM/intent path.
        if recap is None:
            return None

        if recap.patient_id != patient.id:
            log.warning(
                "recap-action cross-patient refused: recap.patient_id=%s patient.id=%s",
                recap.patient_id,
                patient.id,
            )
            return {
                "response_body": (
                    "That recap doesn't belong to your account. "
                    "If this is wrong, reply HELP."
                ),
                "audit_reasons": ["recap_action_cross_patient_refused"],
            }

        if action == "ack":
            await appointment_recaps_repo.mark_acknowledged(db, recap.id)
            await db.commit()
            return {
                "response_body": (
                    "Got it — thanks for confirming. We'll follow up before "
                    "your next visit."
                ),
                "audit_reasons": ["recap_action_ack"],
            }

        # Question path → flag the recap and open an ops ticket so a
        # clinician can reach out.
        await appointment_recaps_repo.mark_questioned(db, recap.id)

        existing = await ops_tickets_repo.find_open_for_patient_category(
            db,
            patient_id=patient_phone,
            category=RECAP_QUESTION_CATEGORY,
        )
        if existing is None:
            await ops_tickets_repo.create(
                db,
                patient_id=patient_phone,
                category=RECAP_QUESTION_CATEGORY,
                priority=RECAP_QUESTION_PRIORITY,
                sla_minutes=RECAP_QUESTION_SLA_MINUTES,
                notes=(
                    f"Patient asked a question about appointment recap "
                    f"#{recap.id}. Original message:\n{(new_user_text or '').strip()}"
                ),
            )
        await db.commit()
        return {
            "response_body": (
                "Thanks — we've passed your question to the care team. "
                "Someone will reach out within 1 day."
            ),
            "audit_reasons": ["recap_action_question"],
        }

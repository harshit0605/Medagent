"""Deterministic handler for caregiver consent replies.

A caregiver was added to a patient's record and is in
``consent_status=pending`` until they reply. They receive (eventually,
once the Meta consent template is approved) a one-time prompt asking
them to confirm or decline. Until that template lands, ops can also
manually drive consent confirmation from the patient detail page —
this handler is the inbound side that closes the loop when the
caregiver themselves replies.

Two equivalent inputs:

1. Marker-prefixed (when a webhook button-id tap is rewritten)::

       [caregiver-action] confirm caregiver_id=12
       [caregiver-action] decline caregiver_id=12

2. Plain text — patient/caregiver typing "YES" / "NO" (or close
   variants like "ok" / "agree" / "decline"). The handler resolves the
   most recent pending caregiver row keyed by the inbound phone;
   that's the caregiver, not the patient (caregivers have their own
   phone in the ``caregivers`` table and may also be patients in
   their own right, but the pending row's phone is what we match).

Behavior:
- ``confirm`` → mark ``consent_status=confirmed``, ``confirmed_by="caregiver_yes_reply"``.
- ``decline`` → mark ``consent_status=declined``.

Cross-patient safety: the caregiver row's ``phone`` must match the
inbound phone, and the row must currently be ``pending`` (we don't
re-confirm an already-acked one or re-decline a declined one — that
would silently overwrite audit state).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import desc, select

from app.db.models import Caregiver
from app.db.repositories import caregivers as caregivers_repo
from app.db.session import get_sessionmaker

log = logging.getLogger(__name__)


# Marker form: ``[caregiver-action] confirm caregiver_id=12``.
_MARKER_RE = re.compile(
    r"^\s*\[caregiver-action\]\s+(?P<action>confirm|decline)\s+"
    r"caregiver_id\s*=\s*(?P<id>\d+)\s*$",
    re.I,
)

# Plain-text recognisers. Tight on purpose — "OK" alone goes to the
# recap_handler (it's the recap-ack copy). Caregivers reply "YES"/"NO"
# explicitly; the handler also accepts a few natural variants but is
# conservative so unrelated chatter doesn't get swallowed.
_CONFIRM_RE = re.compile(
    r"^\s*(?:yes|y|confirm|i (?:confirm|agree)|agreed?|accept(?:ed)?)\s*\.?\s*$",
    re.I,
)
_DECLINE_RE = re.compile(
    r"^\s*(?:no|n|decline(?:d)?|opt out|don'?t (?:want|consent))\s*\.?\s*$",
    re.I,
)


def _parse_action(text: str | None) -> tuple[str | None, int | None]:
    if not text:
        return None, None
    marker = _MARKER_RE.match(text)
    if marker:
        return marker.group("action").lower(), int(marker.group("id"))
    if _CONFIRM_RE.match(text):
        return "confirm", None
    if _DECLINE_RE.match(text):
        return "decline", None
    return None, None


def looks_like_caregiver_action(text: str | None) -> bool:
    """Cheap pre-router predicate. The actual handler is the gate
    that decides whether the inbound is REALLY a caregiver consent
    reply — by looking up a pending caregiver with this phone."""
    action, _ = _parse_action(text or "")
    return action is not None


async def _find_pending_caregiver(
    db, phone: str
) -> Caregiver | None:
    """Most-recent ``pending`` caregiver row keyed by phone. There
    should normally be at most one — caregivers added across multiple
    patients have separate rows but they're typically tied to one
    person's phone, so we resolve by latest-created."""
    stmt = (
        select(Caregiver)
        .where(Caregiver.phone == phone)
        .where(
            Caregiver.consent_status == caregivers_repo.CONSENT_PENDING
        )
        .where(Caregiver.active.is_(True))
        .order_by(desc(Caregiver.created_at))
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def handle_caregiver_action(
    *, sender_phone: str, new_user_text: str | None
) -> dict[str, Any] | None:
    """Process a caregiver consent reply. Returns None when the inbound
    isn't a caregiver action OR the sender has no pending caregiver
    row (so the orchestrator falls through to the normal LLM path)."""
    action, caregiver_id_hint = _parse_action(new_user_text or "")
    if action is None:
        return None

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        if caregiver_id_hint is not None:
            caregiver = await caregivers_repo.get(db, caregiver_id_hint)
        else:
            caregiver = await _find_pending_caregiver(db, sender_phone)
        if caregiver is None:
            # Plain "YES"/"NO" with no pending caregiver context.
            # Fall through so other handlers + the LLM see it.
            return None
        # Cross-phone safety: the marker form trusts the id but we
        # still verify the phone matches so a malicious payload can't
        # confirm consent for someone else's caregiver.
        if caregiver.phone != sender_phone:
            log.warning(
                "caregiver-action phone mismatch: caregiver.phone=%s sender=%s",
                caregiver.phone,
                sender_phone,
            )
            return {
                "response_body": (
                    "We couldn't match that reply to a pending request "
                    "on your number. If you meant to confirm care contact "
                    "for someone else, the patient or care team needs to "
                    "re-add you."
                ),
                "audit_reasons": ["caregiver_action_phone_mismatch"],
            }
        if caregiver.consent_status != caregivers_repo.CONSENT_PENDING:
            # Already responded — idempotent no-op so a stray re-tap
            # doesn't churn the audit log. Tell the caregiver where
            # they stand so the conversation makes sense.
            current = caregiver.consent_status
            return {
                "response_body": (
                    f"Your care-contact request is already marked "
                    f"{current}. No further action needed."
                ),
                "audit_reasons": [
                    f"caregiver_action_already_{current}"
                ],
            }

        if action == "confirm":
            await caregivers_repo.confirm_consent(
                db, caregiver.id, confirmed_by="caregiver_yes_reply"
            )
            await db.commit()
            return {
                "response_body": (
                    "Thanks — you're now set as a care contact. You'll "
                    "receive copies of post-visit recaps. Reply STOP to "
                    "opt out at any time."
                ),
                "audit_reasons": ["caregiver_action_confirmed"],
            }

        # decline
        await caregivers_repo.decline_consent(db, caregiver.id)
        await db.commit()
        return {
            "response_body": (
                "Got it — we won't add you as a care contact. The "
                "patient's care team has been notified."
            ),
            "audit_reasons": ["caregiver_action_declined"],
        }

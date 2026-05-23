"""Inbound STOP / START keyword handler — opt-out / opt-in flow.

WhatsApp Business Solution Provider terms (and most data-protection
regimes) require a patient-initiated path to stop messages. Without
this, a patient who wants the bot to stop can only block the WhatsApp
number, which we never see — outbound keeps firing into the void.

Two matchers + two state mutations:

    looks_like_optout(text)  → patient wants to stop
    looks_like_optin(text)   → previously-opted-out patient wants back in

    handle_optout(...)  → revoke_consent + ack message
    handle_optin(...)   → restore_consent + ack message

Routing precedence:

    The agent_workflow router runs ``looks_like_optout`` and
    ``looks_like_optin`` BEFORE onboarding and BEFORE every action-
    tap matcher. STOP from a half-onboarded patient still works;
    STOP from a patient who's typing dose-action ought to be honoured
    too — STOP wins in any state.

Outbound suppression:

    The dispatcher gates every scheduled-event send on
    ``patient.consent_sms``. Reply-to-inbound paths (the ack message
    here, freeform answers to patient questions) bypass the gate
    because the patient initiated.

Translations:

    Ack messages render against ``patient.preferred_language`` so a
    Hindi patient who types "STOP" gets the Hindi confirmation. The
    matcher is intentionally English-and-Devanagari only — STOP is
    a global WhatsApp convention; we don't try to detect Hindi
    "रुको" because it has too many false-positive uses ("wait a sec").
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.db.repositories import patients as patients_repo
from app.db.session import get_sessionmaker

log = logging.getLogger(__name__)


# STOP-family. Anchored so the WHOLE message must be the keyword (or
# the keyword + a short trailing word like "please"). A patient saying
# "I'll stop the medication" must NOT trigger opt-out. The strictness
# matters more than recall — false positives revoke consent.
_OPTOUT_RE = re.compile(
    r"^\s*(?:"
    r"stop"
    r"|stop\s+(?:please|now|messages?|sending)"
    r"|unsubscribe"
    r"|unsub"
    r"|opt[-\s]?out"
    r"|leave\s+me\s+alone"
    r"|stop\s+messaging\s+me"
    r"|cancel\s+(?:reminders?|messages?|subscription)"
    r"|disable\s+(?:reminders?|messages?)"
    r")\s*[.!]?\s*$",
    re.IGNORECASE,
)

# START-family. Same anchored discipline — "I'd like to start
# exercising" must NOT trigger opt-in.
_OPTIN_RE = re.compile(
    r"^\s*(?:"
    r"start"
    r"|start\s+(?:reminders?|messages?|messaging)"
    r"|subscribe"
    r"|opt[-\s]?in"
    r"|enable\s+reminders?"
    r"|resume\s+(?:reminders?|messages?)"
    r"|begin\s+reminders?"
    r")\s*[.!]?\s*$",
    re.IGNORECASE,
)


def looks_like_optout(text: str | None) -> bool:
    """Cheap pre-check the agent_workflow router uses to short-circuit
    before the LLM intent + safety nodes run. Strict-match only."""
    if not text:
        return False
    return bool(_OPTOUT_RE.match(text))


def looks_like_optin(text: str | None) -> bool:
    """Cheap pre-check for opt-in keywords. Same strict-match policy
    as ``looks_like_optout``."""
    if not text:
        return False
    return bool(_OPTIN_RE.match(text))


# ---- Localised acks --------------------------------------------------------

# Keep slugs stable across languages. Languages without an entry fall
# back to English silently — same convention as onboarding_handler.
_MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "optout_ack": (
            "Got it — you're opted out of reminders and proactive messages. "
            "✓ I won't send anything until you reply START. "
            "You can still message me anytime if you have a question."
        ),
        "optout_already": (
            "You're already opted out. ✓ Reply START whenever you'd like "
            "reminders to resume."
        ),
        "optin_ack": (
            "Welcome back! ✓ Reminders and proactive messages are turned "
            "back on. Reply STOP anytime to opt out again."
        ),
        "optin_not_opted_out": (
            "You're already receiving reminders. ✓ Reply STOP anytime "
            "to opt out."
        ),
    },
    "hi": {
        "optout_ack": (
            "समझ गया — आपको रिमाइंडर और कोई भी प्रोएक्टिव संदेश नहीं भेजा "
            "जाएगा। ✓ जब आप START लिखेंगे, तब फिर से शुरू होंगे। आप कभी "
            "भी सवाल पूछने के लिए मुझे संदेश भेज सकते हैं।"
        ),
        "optout_already": (
            "आप पहले से ऑप्ट-आउट हैं। ✓ रिमाइंडर फिर से शुरू करने के "
            "लिए कभी भी START लिखें।"
        ),
        "optin_ack": (
            "वापस स्वागत है! ✓ रिमाइंडर और प्रोएक्टिव संदेश फिर से चालू "
            "हो गए हैं। कभी भी ऑप्ट-आउट करने के लिए STOP लिखें।"
        ),
        "optin_not_opted_out": (
            "आप पहले से ही रिमाइंडर पा रहे हैं। ✓ ऑप्ट-आउट करने के लिए "
            "कभी भी STOP लिखें।"
        ),
    },
}


def _render(slug: str, language: str | None) -> str:
    table = _MESSAGES.get((language or "en")) or _MESSAGES["en"]
    return table.get(slug) or _MESSAGES["en"][slug]


# ---- Handler ---------------------------------------------------------------


async def handle_optout(
    *, patient_phone: str, new_user_text: str
) -> dict[str, Any] | None:
    """Process a STOP-keyword inbound. Revokes consent + returns an
    ack-message delta. Returns ``None`` only if the patient row is
    missing (defensive — upsert_patient runs upstream)."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        patient = await patients_repo.get_by_phone(db, patient_phone)
        if patient is None:
            log.warning(
                "optout handler: patient %s not found", patient_phone
            )
            return None

        lang = getattr(patient, "preferred_language", None) or "en"
        already_opted_out = (
            getattr(patient, "consent_sms", False) is False
            and getattr(patient, "consent_revoked_at", None) is not None
        )

        if already_opted_out:
            # No state change but still send the ack so the patient
            # gets confirmation that we received the message.
            log.info(
                "optout: patient %s already opted out — re-acking",
                patient_phone,
            )
            return _reply(
                _render("optout_already", lang),
                audit_codes=["optout_already"],
            )

        await patients_repo.revoke_consent(
            db, patient.id, reason="patient_stop_keyword"
        )
        await db.commit()
        log.info(
            "optout: patient %s opted out via keyword %r",
            patient_phone,
            (new_user_text or "").strip()[:40],
        )
        return _reply(
            _render("optout_ack", lang),
            audit_codes=["optout"],
        )


async def handle_optin(
    *, patient_phone: str, new_user_text: str
) -> dict[str, Any] | None:
    """Process a START-keyword inbound. Restores consent + returns an
    ack-message delta. If the patient was never opted out, returns a
    gentle "you're already subscribed" reply."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        patient = await patients_repo.get_by_phone(db, patient_phone)
        if patient is None:
            log.warning(
                "optin handler: patient %s not found", patient_phone
            )
            return None

        lang = getattr(patient, "preferred_language", None) or "en"
        was_opted_out = (
            getattr(patient, "consent_sms", False) is False
            and getattr(patient, "consent_revoked_at", None) is not None
        )

        if not was_opted_out:
            return _reply(
                _render("optin_not_opted_out", lang),
                audit_codes=["optin_not_opted_out"],
            )

        await patients_repo.restore_consent(db, patient.id)
        await db.commit()
        log.info(
            "optin: patient %s opted back in via keyword %r",
            patient_phone,
            (new_user_text or "").strip()[:40],
        )
        return _reply(
            _render("optin_ack", lang),
            audit_codes=["optin"],
        )


def _reply(body: str, *, audit_codes: list[str]) -> dict[str, Any]:
    return {
        "response_body": body,
        "audit_reasons": audit_codes,
        "intent": "general_question",
        "risk_level": "low",
        "escalation_required": False,
        "use_template": False,
        "template_name": None,
        "quick_replies": [],
        "buttons": [],
        "list_rows": [],
        "list_button_label": None,
        "list_section_title": None,
    }

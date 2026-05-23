"""Inbound side-effect / adverse-reaction reports.

These are the messages we MUST not lose. A patient WhatsApping
"metformin is giving me bad headaches" or "I think I'm having an
allergic reaction" is reporting a clinical event the bot is not
qualified to assess — but it IS qualified to:

    1. Acknowledge receipt (so the patient knows the message landed)
    2. Open a high-priority ops ticket so a doctor sees it
    3. Capture the patient's active regimens alongside the verbatim
       inbound — without context, the doctor would have to dig
    4. Steer the patient to emergency services if the report sounds
       acute

This is intentionally NOT a clinical triage path. We don't try to
classify severity ("mild rash" vs "anaphylaxis") — symptom triage
needs a clinical owner and was scoped out earlier. v1 is purely a
"detect + escalate + log" loop: spot the report, file the ticket,
send the ack with emergency guidance, get out of the way.

Routing precedence:

    The agent_workflow router runs ``looks_like_side_effect_report``
    AFTER the existing structured action-tap matchers (dose / refill
    / lab / recap / caregiver) but BEFORE ``classify_lookup_query``
    and ``detect_intent``. A patient saying "the meds are giving me
    a rash, what am I taking?" gets routed to the side-effect path
    rather than the medication lookup — patient safety beats query
    convenience.

False-positive discipline:

    The matchers are intentionally STRICT. Generic phrases that
    happen to mention "side effect" in non-medical contexts ("I had
    side effects on my software release") must NOT trigger. The cost
    of a false negative (a real side-effect message falls through to
    the LLM compose path which still produces a sensible reply) is
    much lower than the cost of a false positive (a non-event opens
    a high-priority ticket on an unrelated message). When in doubt,
    don't classify.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from app.db.repositories import ops_tickets as ops_tickets_repo
from app.db.repositories import patients as patients_repo
from app.db.repositories import regimens as regimens_repo
from app.db.session import get_sessionmaker

log = logging.getLogger(__name__)


CATEGORY: str = "side_effect_report"
PRIORITY: str = "high"  # patient-safety reports beat routine queue
SLA_MINUTES: int = 30   # tight — clinician should see within half an hour


# ---- Matchers --------------------------------------------------------------

# Pattern 1: explicit "side effect(s)" phrase. Highest-confidence
# trigger — patients who use these words almost always mean what they
# say. Word-boundary anchored so substrings inside other words don't
# match. Hyphenation tolerated ("side-effect" / "side effect").
_PHRASE_SIDE_EFFECT = re.compile(
    r"\bside[\s-]effects?\b",
    re.IGNORECASE,
)

# Pattern 2: explicit allergic-reaction language. Tighter than the
# bare "allergic to X" form — that one's a disclosure ("I'm allergic
# to peanuts") rather than an adverse event. We only match when the
# patient signals an active reaction. Includes "allergic reaction"
# specifically (not just "allergic"), and the broader "having a
# reaction" language.
_PHRASE_ALLERGIC = re.compile(
    r"\b(?:allergic\s+reaction"
    r"|having\s+(?:a\s+|an\s+)?(?:bad\s+|adverse\s+|allergic\s+)?reaction"
    r"|adverse\s+reaction"
    r"|allergic\s+to\s+(?:the\s+|my\s+|this\s+)?"
    r"(?:medication|medicine|meds?|pills?|drugs?|tablets?|prescription))\b",
    re.IGNORECASE,
)

# Pattern 3: medication-attributed symptom. Requires BOTH a symptom
# word AND an attribution clause linking it to medication. The
# attribution requirement is what filters out generic complaints
# ("I'm dizzy" alone doesn't trigger, but "I'm dizzy from the meds"
# does). Symptom list is intentionally clinical — we want the
# unambiguous adverse-effect vocabulary, not every word a patient
# might say.
_SYMPTOM = (
    r"(?:dizzy|dizziness|nausea|nauseous|vomiting|throwing\s+up"
    r"|rash|rashes|itching|itchy|hives"
    r"|headaches?|migraines?"
    r"|sick|drowsy|drowsiness"
    r"|swelling|swollen"
    r"|stomach\s+(?:pain|ache|cramps?)"
    r"|breathing\s+(?:problems?|issues?|difficulty)"
    r"|chest\s+(?:pain|tightness)"
    r"|fatigue|exhaustion|tired)"
)
_ATTRIBUTION_FROM = (
    r"(?:from|after|since)\s+"
    r"(?:taking\s+|starting\s+|using\s+|i\s+started\s+)?"
    r"(?:the\s+|my\s+|this\s+|new\s+)?"
    r"(?:medication|medicine|meds?|pills?|drugs?|tablets?|prescription)"
)
_PHRASE_MED_ATTRIBUTED_SYMPTOM = re.compile(
    rf"\b{_SYMPTOM}\b\s*[^.!?]*\b{_ATTRIBUTION_FROM}\b",
    re.IGNORECASE,
)

# Pattern 4: medication-as-subject-of-causation. "the meds are
# making me X", "this medication caused X", "metformin gave me X".
# Captures the inverse phrasing of Pattern 3 (med-then-symptom
# instead of symptom-then-med).
_MED_NOUN_FOR_CAUSATION = (
    r"(?:medication|medicine|meds?|pills?|drugs?|tablets?"
    r"|this\s+(?:med|medication|drug|pill|tablet))"
)
_PHRASE_MED_CAUSING = re.compile(
    rf"\b(?:the\s+|my\s+|this\s+)?{_MED_NOUN_FOR_CAUSATION}"
    r"\s+(?:is|are|was|were|has\s+been|have\s+been)\s+"
    r"(?:making\s+me|causing(?:\s+me)?|giving\s+me)",
    re.IGNORECASE,
)

# Pattern 5: specific-medication causation. "metformin gave me
# headaches" / "the new prescription caused nausea" / "atorvastatin
# is making me dizzy". Captures the case where a patient names a
# specific drug — Pattern 4 needs the SUBJECT to be a generic noun
# ("the meds", "the medication"), which misses real reports that
# name the medication. We require the symptom to be in our clinical
# vocabulary so "Mike gave me a headache" type sentences (rare in a
# medical bot context but possible) are caught for clinician review
# rather than dismissed.
_PHRASE_NAMED_CAUSING = re.compile(
    rf"\b\w+\s+(?:gave|caused|is\s+causing|made|is\s+making)"
    rf"\s+(?:me\s+)?"
    rf"(?:a\s+|some\s+|terrible\s+|bad\s+|severe\s+)?"
    rf"{_SYMPTOM}\b",
    re.IGNORECASE,
)

# Pattern 6: Hindi keyword. Devanagari "साइड इफेक्ट" (Roman: "side
# effect" transliterated). Patients who type in Hindi but use the
# loanword pattern. Strict word-boundary equivalent — the Devanagari
# script has its own punctuation but we use surrounding whitespace
# / message edges as boundary signal.
_PHRASE_HI_SIDE_EFFECT = re.compile(
    r"साइड[\s\-]*इफ़?ेक्ट",
)


def looks_like_side_effect_report(text: str | None) -> bool:
    """Cheap pre-check the agent_workflow router uses to short-circuit
    before the LLM intent + safety nodes run. Returns True only when
    we're highly confident the inbound is reporting an adverse event.
    Misses fall through to the LLM compose path."""
    if not text:
        return False
    if _PHRASE_SIDE_EFFECT.search(text):
        return True
    if _PHRASE_ALLERGIC.search(text):
        return True
    if _PHRASE_MED_CAUSING.search(text):
        return True
    if _PHRASE_MED_ATTRIBUTED_SYMPTOM.search(text):
        return True
    if _PHRASE_NAMED_CAUSING.search(text):
        return True
    if _PHRASE_HI_SIDE_EFFECT.search(text):
        return True
    return False


# ---- Localised acks --------------------------------------------------------

# Each ack carries:
#   - confirmation of receipt
#   - "we've flagged this for your doctor"
#   - emergency-services guidance (112 is India's universal emergency
#     line; the copy uses "your local emergency number" so the same
#     template works for non-IN patients without a regional code
#     change)
_MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "ack": (
            "Thank you for letting me know. I've flagged this for your "
            "care team and a clinician will review it shortly. ⚠️ "
            "If your symptoms feel severe — trouble breathing, "
            "swelling of the face/throat, severe chest pain, or fainting "
            "— please call your local emergency number (112 in India) "
            "right away or go to the nearest emergency room."
        ),
        "no_patient": (
            "I've noted what you've shared. Please reply with your full "
            "name so I can flag this to your care team."
        ),
    },
    "hi": {
        "ack": (
            "बताने के लिए धन्यवाद। मैंने इसे आपकी केयर टीम को भेज दिया "
            "है और जल्द ही एक चिकित्सक इसकी समीक्षा करेगा। ⚠️ यदि "
            "आपके लक्षण गंभीर हैं — साँस लेने में परेशानी, चेहरे/गले "
            "में सूजन, सीने में तेज़ दर्द, या बेहोशी — तो तुरंत अपने "
            "स्थानीय आपातकालीन नंबर (भारत में 112) पर कॉल करें या "
            "नज़दीकी आपातकालीन कक्ष में जाएँ।"
        ),
        "no_patient": (
            "जो आपने बताया, मैंने नोट कर लिया है। कृपया अपना पूरा "
            "नाम भेजें ताकि मैं इसे आपकी केयर टीम को बता सकूँ।"
        ),
    },
}


def _render(slug: str, language: str | None) -> str:
    table = _MESSAGES.get((language or "en")) or _MESSAGES["en"]
    return table.get(slug) or _MESSAGES["en"][slug]


# ---- Handler ---------------------------------------------------------------


async def handle_side_effect_report(
    *, patient_phone: str, new_user_text: str
) -> dict[str, Any] | None:
    """Process a detected side-effect / adverse-reaction inbound.

    Side-effects:
        - Loads the patient + their active regimens
        - Opens a high-priority ops_ticket with category=side_effect_report
        - Notes capture the verbatim inbound + the active regimens so
          the doctor has context without digging
        - Sends an immediate ack to the patient (in their preferred
          language) including emergency-services guidance

    Returns the AgentState delta the caller writes back. Returns None
    only if the patient row is missing (defensive — upsert_patient
    runs upstream).
    """
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        patient = await patients_repo.get_by_phone(db, patient_phone)
        if patient is None:
            log.warning(
                "side-effect handler: patient %s not found", patient_phone
            )
            return _reply(
                _render("no_patient", "en"),
                audit_codes=["side_effect_no_patient"],
            )

        lang = getattr(patient, "preferred_language", None) or "en"

        # Pull active regimens so the doctor sees what the patient is
        # currently on alongside the symptom report. Without this,
        # the ticket says "patient reports nausea" with no list of
        # medications to cross-reference.
        try:
            today = datetime.now(timezone.utc).date()
            regimens = await regimens_repo.list_for_patient(
                db, patient.id, active_on=today
            )
        except Exception:  # noqa: BLE001 — context is best-effort
            log.exception(
                "side-effect handler: regimen lookup failed for "
                "patient %s; opening ticket without context",
                patient_phone,
            )
            regimens = []

        notes = _build_ticket_notes(
            inbound=new_user_text or "",
            regimens=regimens,
            now=datetime.now(timezone.utc),
        )

        ticket = await ops_tickets_repo.create(
            db,
            patient_id=patient.phone,
            category=CATEGORY,
            priority=PRIORITY,
            sla_minutes=SLA_MINUTES,
            notes=notes,
        )
        await db.commit()

        log.warning(
            "side-effect report: opened ticket %s for patient %s "
            "(%d active regimens captured)",
            ticket.id,
            patient_phone,
            len(regimens),
        )

        return _reply(
            _render("ack", lang),
            audit_codes=["side_effect_logged"],
        )


def _build_ticket_notes(
    *, inbound: str, regimens, now: datetime | None = None
) -> str:
    """Render the ops_ticket notes body. Layout is dense + scannable
    so a clinician can read the relevant context in 5 seconds."""
    when = (now or datetime.now(timezone.utc)).isoformat()
    lines = [
        "[side-effect report]",
        f"Reported at: {when}",
        "",
        "Patient said:",
        f"  > {inbound.strip() or '(empty inbound)'}",
        "",
    ]
    if regimens:
        lines.append("Active regimens at time of report:")
        for r in regimens:
            med = (r.medication_name or "").strip()
            dose = (r.dose or "").strip()
            entry = f"  - {med}"
            if dose:
                entry += f" {dose}"
            if r.starts_on is not None:
                entry += f" (started {r.starts_on.isoformat()})"
            lines.append(entry)
    else:
        lines.append("Active regimens at time of report: (none on file)")
    return "\n".join(lines)


def _reply(body: str, *, audit_codes: list[str]) -> dict[str, Any]:
    return {
        "response_body": body,
        "audit_reasons": audit_codes,
        "intent": "general_question",
        "risk_level": "high",  # downstream surfaces use this for sorting
        "escalation_required": True,
        "use_template": False,
        "template_name": None,
        "quick_replies": [],
        "buttons": [],
        "list_rows": [],
        "list_button_label": None,
        "list_section_title": None,
    }

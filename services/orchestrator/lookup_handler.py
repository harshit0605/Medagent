"""Patient self-service lookup queries — "what meds am I on" / "what
labs do I have".

The bot already has all the patient's data (regimens, lab_followups,
adherence_events) but no inbound path turns "list my medications"
into a structured response. The freeform LLM compose path doesn't
have access to the database — it would either refuse or hallucinate
a response with made-up medication names.

This handler short-circuits two narrow query intents BEFORE the LLM
runs, queries the patient's own data deterministically, and renders
a structured response in the patient's preferred_language. Anything
the classifier doesn't recognise falls through unchanged to the
existing detect_intent → policy → safety → compose pipeline.

Intentionally NOT included: appointment lookup. The booking_agent
already handles "when's my next appointment" via its
``find_existing_appointment_by_phrase`` tool, and we don't want two
paths competing for the same query.

Routing:

    The agent_workflow router calls ``classify_lookup_query`` after
    onboarding/optout/action-tap matchers but BEFORE detect_intent.
    A non-None return routes to ``lookup_handler``; None falls
    through to the existing intent pipeline.

Localisation:

    Templates render against ``patient.preferred_language`` (English
    fallback for languages without a translation entry — same
    convention as ``onboarding_handler``).
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone
from typing import Any, Literal

from app.db.models import FollowupStatus
from app.db.repositories import lab_followups as lab_followups_repo
from app.db.repositories import patients as patients_repo
from app.db.repositories import regimens as regimens_repo
from app.db.session import get_sessionmaker

log = logging.getLogger(__name__)


LookupQueryType = Literal["medications", "labs"]


# ---- Classifier ------------------------------------------------------------

# High-precision matchers. False positives are worse than false negatives
# here — a missed query falls through to the LLM (which gracefully
# explains it can't answer); a false positive would short-circuit a
# different question into a structured response that doesn't match it.
#
# Anchored at start of message, optional trailing punctuation. Keyword
# requires question-shape ("what" / "show" / "list" / "tell me" / "do I"),
# OR a possessive form ("my meds", "my labs"). Bare nouns ("medications")
# don't match — too ambiguous.
# ``_MED_NOUN`` shared so the alternatives stay aligned when we extend
# the noun list. Each alternative covers a distinct query SHAPE:
#   - "what (are my)? meds"                     — bare-question form
#   - "what meds am I (on|taking)"              — noun-then-verb
#   - "what am I taking"                        — verb-only (no noun)
#   - "show/list/tell me (my)? meds"            — imperative
#   - "do I have any meds"                      — yes/no-question
#   - "my meds" / "current meds"                — possessive shorthand
_MED_NOUN = r"(?:medications?|medicines?|meds?|prescriptions?|drugs?)"
_MEDICATIONS_RE = re.compile(
    r"^\s*(?:"
    rf"what(?:'s|\s+are|\s+is)?\s+(?:my\s+)?{_MED_NOUN}"
    rf"|what\s+{_MED_NOUN}\s+(?:am\s+i|do\s+i)\s+(?:take|taking|on)"
    r"|what\s+(?:am\s+i|do\s+i)\s+(?:take|taking|on)"
    rf"|(?:show|list|tell\s+me)\s+(?:my\s+)?{_MED_NOUN}"
    rf"|do\s+i\s+have\s+any\s+{_MED_NOUN}"
    rf"|my\s+{_MED_NOUN}"
    rf"|(?:current|active)\s+{_MED_NOUN}"
    r")"
    r"\s*[?.!]?\s*$",
    re.IGNORECASE,
)

_LAB_NOUN = r"(?:labs?|lab\s+tests?|tests?|blood\s+tests?|blood\s+work)"
_LABS_RE = re.compile(
    r"^\s*(?:"
    rf"what(?:'s|\s+are|\s+is)?\s+(?:my\s+)?{_LAB_NOUN}"
    rf"|what\s+{_LAB_NOUN}\s+(?:do\s+i\s+have|are\s+due|am\s+i\s+(?:owed|waiting\s+for))"
    rf"|(?:show|list|tell\s+me)\s+(?:my\s+)?{_LAB_NOUN}"
    rf"|do\s+i\s+have\s+any\s+{_LAB_NOUN}"
    rf"|(?:upcoming|due|pending|outstanding)\s+{_LAB_NOUN}"
    rf"|my\s+{_LAB_NOUN}"
    r"|labs?\s+due"
    r"|tests?\s+due"
    r")"
    r"\s*[?.!]?\s*$",
    re.IGNORECASE,
)


def classify_lookup_query(text: str | None) -> LookupQueryType | None:
    """Return ``"medications"``, ``"labs"``, or ``None``. Mutually
    exclusive — a query can only match one bucket. The regexes are
    anchored start-and-end so prefix text ("my labs are at X clinic")
    won't classify, only direct lookup intents."""
    if not text:
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    if _MEDICATIONS_RE.match(cleaned):
        return "medications"
    if _LABS_RE.match(cleaned):
        return "labs"
    return None


# ---- Localised copy --------------------------------------------------------

_MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "no_patient": (
            "I couldn't find your profile. Please reply with your full "
            "name first to set things up."
        ),
        "meds_header": "Your active medications:",
        "meds_empty": (
            "You don't have any active medications on file right now. If "
            "you've been prescribed something recently, your care team "
            "will add it once it's reviewed."
        ),
        "labs_header": "Your lab tests:",
        "labs_empty": (
            "You don't have any pending lab tests on file. ✓"
        ),
    },
    "hi": {
        "no_patient": (
            "मुझे आपकी प्रोफ़ाइल नहीं मिली। कृपया पहले अपना पूरा नाम "
            "भेजकर सेटअप पूरा करें।"
        ),
        "meds_header": "आपकी मौजूदा दवाइयाँ:",
        "meds_empty": (
            "इस समय आपके रिकॉर्ड में कोई सक्रिय दवा नहीं है। यदि हाल "
            "में कोई दवा निर्धारित की गई है, तो समीक्षा के बाद आपकी "
            "केयर टीम उसे जोड़ देगी।"
        ),
        "labs_header": "आपके लैब टेस्ट:",
        "labs_empty": "आपके रिकॉर्ड में कोई पेंडिंग लैब टेस्ट नहीं है। ✓",
    },
}


def _render(slug: str, language: str | None) -> str:
    table = _MESSAGES.get((language or "en")) or _MESSAGES["en"]
    return table.get(slug) or _MESSAGES["en"][slug]


# ---- Renderers -------------------------------------------------------------

# Status localisation. Falls back to the Python enum value when the
# language doesn't translate it — at least the patient sees a
# recognisable token rather than crashing the render.
_STATUS_LABELS: dict[str, dict[str, str]] = {
    "en": {
        "due": "due",
        "booked": "booked",
        "completed": "completed",
        "reviewed": "reviewed",
    },
    "hi": {
        "due": "बाक़ी",
        "booked": "बुक हो चुका",
        "completed": "पूर्ण",
        "reviewed": "समीक्षा हो चुकी",
    },
}


def _status_label(status: FollowupStatus, language: str) -> str:
    table = _STATUS_LABELS.get(language) or _STATUS_LABELS["en"]
    return table.get(status.value, status.value)


def _render_medications(regimens, language: str) -> str:
    """Render a bulleted active-regimen list. The schedule JSON is
    intentionally NOT formatted here — its shape varies (twice-daily,
    once-daily, every-other-day) and adding a generic schedule
    formatter is out of scope for v1. Patients who want timing detail
    can ask follow-up questions through the LLM compose path."""
    if not regimens:
        return _render("meds_empty", language)
    lines = [_render("meds_header", language)]
    for r in regimens:
        # "• Metformin 500 mg" — keep it terse so the WhatsApp bubble
        # stays readable on small screens.
        med = (r.medication_name or "").strip()
        dose = (r.dose or "").strip()
        if dose:
            lines.append(f"• {med} {dose}")
        else:
            lines.append(f"• {med}")
    return "\n".join(lines)


def _render_labs(labs, language: str) -> str:
    """Render a bulleted lab list — pending (due/booked) only. Newest
    due-by first; rows without due_by sort to the bottom."""
    pending = [
        lab
        for lab in labs
        if lab.status in (FollowupStatus.due, FollowupStatus.booked)
    ]
    if not pending:
        return _render("labs_empty", language)
    pending.sort(
        key=lambda r: (r.due_by is None, r.due_by or date.max)
    )
    lines = [_render("labs_header", language)]
    for lab in pending:
        status = _status_label(lab.status, language)
        if lab.due_by is not None:
            lines.append(
                f"• {lab.test_name} — {status} (by {lab.due_by.isoformat()})"
            )
        else:
            lines.append(f"• {lab.test_name} — {status}")
    return "\n".join(lines)


# ---- Handler ---------------------------------------------------------------


async def handle_lookup_query(
    *,
    patient_phone: str,
    query_type: LookupQueryType,
    new_user_text: str = "",
) -> dict[str, Any] | None:
    """Process a self-service lookup. Returns a state delta with the
    rendered response. Returns None ONLY if the patient row is missing
    (defensive — upsert_patient runs upstream)."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        patient = await patients_repo.get_by_phone(db, patient_phone)
        if patient is None:
            log.warning(
                "lookup handler: patient %s not found", patient_phone
            )
            return _reply(
                _render("no_patient", "en"),
                audit_codes=["lookup_no_patient"],
            )

        lang = getattr(patient, "preferred_language", None) or "en"

        if query_type == "medications":
            today = datetime.now(timezone.utc).date()
            regimens = await regimens_repo.list_for_patient(
                db, patient.id, active_on=today
            )
            body = _render_medications(regimens, lang)
            return _reply(
                body,
                audit_codes=["lookup_medications"],
            )

        if query_type == "labs":
            labs = await lab_followups_repo.list_for_patient(
                db, patient.id, limit=50
            )
            body = _render_labs(labs, lang)
            return _reply(
                body,
                audit_codes=["lookup_labs"],
            )

        # Should be unreachable — Literal type narrows to the two
        # cases above. Defensive None so a future query type added
        # to the Literal but not implemented here doesn't crash.
        log.warning(
            "lookup handler: unknown query_type %r", query_type
        )
        return None


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

"""Deterministic onboarding state machine for first-time patients.

Sits in the LangGraph workflow right after ``upsert_patient`` and BEFORE
button-action / intent routing. While ``patients.onboarding_step`` is not
``done``, every inbound is consumed by the onboarding handler — the
patient walks through name → cohorts → consent before any other flow
(booking, dose buttons, etc.) can run.

State transitions:

    pending        → send greeting, advance to needs_name
    needs_name     → store full_name, ask cohorts, advance to needs_cohorts
    needs_cohorts  → parse cohorts, ask consent, advance to needs_consent
    needs_consent  → parse YES/NO, set consent_sms, send "done", advance to done
    done           → not handled here (falls through)

All parsing is permissive — anything we can't classify goes to a
"sorry, didn't catch that" prompt that re-asks the same question rather
than crashing or skipping ahead. Critically, ``parse_cohorts`` returns
``None`` (not ``{all-False}``) for unparseable input — the previous
silent-commit behaviour lost cohort flags for patients who typed
ambiguous replies.

Outbound copy is rendered against ``patients.preferred_language`` via
``_MESSAGES``. Languages without a translation entry fall back to
English — adding one is a constants-only change.

Two safety nets sit on top of the parser logic:

* **Escalation** — every re-prompt bumps ``onboarding_retry_count``.
  After ``ESCALATION_THRESHOLD`` consecutive failures at the same
  step we open an ``onboarding_stuck`` ops ticket and switch the
  reply to a "teammate will reach out" message so the patient
  isn't stuck looping with the bot.
* **Stale reset** — if the patient ghosted the flow for
  ``STALE_AFTER_DAYS`` days the next inbound resets them to
  ``pending`` and re-greets from scratch instead of resuming a
  context they've forgotten.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from app.db.repositories import ops_tickets as ops_tickets_repo
from app.db.repositories import patients as patients_repo
from app.db.session import get_sessionmaker

log = logging.getLogger(__name__)


# After this many consecutive re-prompts at the same step we open an
# ops ticket and stop sending the "didn't catch that" copy in favour of
# a "teammate will reach out" message. 3 keeps the patience generous
# without burning support hours on classifier corner cases.
ESCALATION_THRESHOLD: int = 3
ESCALATION_CATEGORY: str = "onboarding_stuck"
ESCALATION_PRIORITY: str = "medium"
# 24h SLA — onboarding-stuck isn't safety-critical (patient hasn't
# completed profile, hasn't started any care plan). Generous window
# avoids paging for what's usually a confused first-time user.
ESCALATION_SLA_MINUTES: int = 24 * 60

# Patients who haven't advanced in this many days get reset to
# ``pending`` on next inbound. The previous half-onboarded state is
# preserved in ops history but a fresh greeting is more useful than
# resuming a flow they've forgotten.
STALE_AFTER_DAYS: int = 30


PENDING = "pending"
NEEDS_NAME = "needs_name"
NEEDS_COHORTS = "needs_cohorts"
NEEDS_CONSENT = "needs_consent"
DONE = "done"


_ACTIVE_STEPS: frozenset[str] = frozenset(
    {PENDING, NEEDS_NAME, NEEDS_COHORTS, NEEDS_CONSENT}
)


def is_onboarding_active(step: str | None) -> bool:
    """Caller (the workflow router) uses this to decide whether to
    short-circuit to the onboarding handler."""
    return step in _ACTIVE_STEPS


# ---- Name validation ------------------------------------------------------

# The pre-handler router already filters action-tap markers to their own
# nodes, but a stale ``needs_name`` patient who somehow ends up here with
# one of these inputs would otherwise get the marker written as their
# full_name. Reject defensively.
_ACTION_MARKER_RE = re.compile(
    r"^\s*\[(?:dose|refill|lab|recap|caregiver|prescription)-?\w*\]",
    re.I,
)
# All-numeric / mostly-symbols inputs are not real names. Single-letter
# replies (``"x"``) are also not useful and re-prompt.
_MIN_NAME_LEN = 2
_MAX_NAME_LEN = 100
# Latin (incl. accented) + the Indic scripts our supported languages
# use. Explicit \u ranges to keep the codepoints reviewable.
_NAME_HAS_LETTER_RE = re.compile(
    r"[A-Za-z"
    r"À-ɏ"  # Latin Extended (À–ɏ — accented chars)
    r"ऀ-ॿ"  # Devanagari (Hindi, Marathi)
    r"ঀ-৿"  # Bengali
    r"਀-੿"  # Gurmukhi (Punjabi)
    r"઀-૿"  # Gujarati
    r"஀-௿"  # Tamil
    r"ఀ-౿"  # Telugu
    r"ಀ-೿"  # Kannada
    r"ഀ-ൿ"  # Malayalam
    r"]"
)


def validate_name(text: str | None) -> str | None:
    """Return a cleaned name or ``None`` if the input isn't acceptable.

    Rejects: empty, action-tap markers, all-digit/all-symbol replies,
    extremely short single-character inputs.
    """
    if not text:
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    if _ACTION_MARKER_RE.match(cleaned):
        return None
    if len(cleaned) < _MIN_NAME_LEN:
        return None
    # Must contain at least one letter (Latin or common Indic scripts).
    # Pure-numeric "9876543210" or "@@@" replies re-prompt.
    if not _NAME_HAS_LETTER_RE.search(cleaned):
        return None
    return cleaned[:_MAX_NAME_LEN]


# ---- Cohort parsing --------------------------------------------------------

_COHORT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "diabetes": ("diabetes", "diabetic", "sugar", "1"),
    "cardiac": ("heart", "cardiac", "cardio", "2"),
    "fall_risk": ("fall", "fall risk", "balance", "3"),
}
_NONE_RE = re.compile(r"\bnone\b|\bno\b|^\s*4\s*$", re.I)


def parse_cohorts(text: str) -> dict[str, bool] | None:
    """Return a flag dict for the three cohort columns.

    Returns:
        ``{cohort: bool, ...}`` — cohorts the patient picked.
        ``None``                 — input was unparseable (caller should re-prompt).

    Recognises numeric picks (``1, 3``), keywords (``diabetes, fall``),
    and explicit "none" / ``4`` / ``no``. Anything else (random text,
    non-cohort words) returns ``None`` so the handler re-asks instead
    of silently committing all-False — that previous behaviour was a
    silent data-loss bug for patients who typed unusual replies.
    """
    out = {"diabetes": False, "cardiac": False, "fall_risk": False}
    if text is None:
        return None
    cleaned = text.strip()
    if not cleaned:
        return None
    lower = cleaned.lower()
    if _NONE_RE.search(lower):
        return out
    matched = False
    for key, keywords in _COHORT_KEYWORDS.items():
        for kw in keywords:
            if kw.isdigit():
                if re.search(rf"(^|[^\d]){kw}([^\d]|$)", lower):
                    out[key] = True
                    matched = True
                    break
            elif re.search(rf"\b{re.escape(kw)}\b", lower):
                out[key] = True
                matched = True
                break
    if not matched:
        return None
    return out


# ---- Consent parsing -------------------------------------------------------

_YES_RE = re.compile(
    r"\b(?:yes|y|yeah|yep|sure|ok|okay|please|go ahead|sounds good|fine)\b",
    re.I,
)
_NO_RE = re.compile(r"\b(?:no|n|nope|not now|skip|cancel|stop)\b", re.I)
# Patterns that should NEVER classify as yes/no — even though they may
# contain yes/no keywords, the patient is signalling uncertainty.
_AMBIGUOUS_RE = re.compile(
    r"\b(?:not sure|maybe|idk|unsure|hmm+|probably|perhaps|"
    r"i don'?t know|dunno|let me think)\b",
    re.I,
)


def parse_consent(text: str) -> bool | None:
    """Return True for yes, False for no, None for unparseable.

    Ambiguous phrases (``"not sure"``, ``"maybe"``, etc.) override both
    yes and no detection — without this, ``"not sure"`` would match the
    yes regex via ``sure`` and the patient's hesitation would be silently
    treated as consent."""
    if not text:
        return None
    if _AMBIGUOUS_RE.search(text):
        return None
    if _YES_RE.search(text):
        return True
    if _NO_RE.search(text):
        return False
    return None


# ---- Localised copy --------------------------------------------------------

# Each entry is keyed by message slug. Slots in ``{}``-curlies are
# substituted from kwargs. Languages without an entry fall back to
# English. Keep slugs stable; the audit_codes still use the English
# slug regardless of rendered language.
_MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "greeting": (
            "Hi! I'm your care assistant on WhatsApp. I'll help with "
            "appointments, medication reminders, refills, and lab "
            "follow-ups. To set up your profile, what's your full name?"
        ),
        "name_invalid": (
            "I didn't catch a name there. Could you reply with your "
            "full name (letters only)?"
        ),
        "cohorts_prompt": (
            "Thanks {name}! Do any of these apply to you? Reply with "
            "the ones you have (e.g. '1, 3' or 'diabetes, fall risk') "
            "— or 'none':\n\n"
            "1. Diabetes\n"
            "2. Heart condition\n"
            "3. Fall risk\n"
            "4. None of these"
        ),
        "cohorts_unclear": (
            "I didn't catch your selection. Reply with the numbers or "
            "keywords for any that apply (e.g. '1, 3' or 'diabetes, "
            "fall risk') — or '4' / 'none' if none apply."
        ),
        "consent_prompt": (
            "Got it — recorded: {picked_label}. One last thing — is it "
            "OK to send you reminders here for medications, "
            "appointments, refills, and labs? Reply YES or NO."
        ),
        "consent_unclear": (
            "Sorry, I didn't catch that. Reply YES if it's OK to send "
            "reminders here, or NO to opt out."
        ),
        "complete_yes": (
            "All set! ✓ I'll send reminders here as needed. You can "
            "ask me to book appointments, check upcoming visits, or "
            "log medication adherence anytime."
        ),
        "complete_no": (
            "Got it — no reminders for now. ✓ You can still message "
            "me anytime to book appointments or ask questions, and "
            "you can opt back in by saying 'enable reminders'."
        ),
        "label_none": "none",
        # Sent once the retry counter crosses ESCALATION_THRESHOLD.
        # Replaces the standard "didn't catch that" re-prompt so the
        # patient knows a human is being looped in rather than looping
        # with the bot.
        "escalated": (
            "Sorry I'm having trouble getting your details. A teammate "
            "will reach out shortly to help finish setting up your "
            "profile. You don't need to do anything else for now."
        ),
    },
    # Hindi (Devanagari). Keeps cohort numbers + "none" / yes-no
    # English-recognisable so existing parsers keep working — the
    # patient sees Hindi prose but their reply syntax is unchanged.
    "hi": {
        "greeting": (
            "नमस्ते! मैं WhatsApp पर आपका केयर असिस्टेंट हूँ। "
            "अपॉइंटमेंट, दवा रिमाइंडर, रीफिल और लैब फ़ॉलो-अप में मदद "
            "करूँगा। अपनी प्रोफ़ाइल सेट करने के लिए, कृपया अपना "
            "पूरा नाम बताएँ।"
        ),
        "name_invalid": (
            "मुझे नाम समझ नहीं आया। कृपया अपना पूरा नाम (केवल अक्षरों "
            "में) भेजें।"
        ),
        "cohorts_prompt": (
            "धन्यवाद {name}! क्या इनमें से कोई आप पर लागू होता है? "
            "जो भी लागू हों उनका नंबर या नाम भेजें (जैसे '1, 3' या "
            "'diabetes, fall risk') — या 'none' लिखें:\n\n"
            "1. मधुमेह (Diabetes)\n"
            "2. हृदय रोग (Heart condition)\n"
            "3. गिरने का जोखिम (Fall risk)\n"
            "4. इनमें से कोई नहीं (None)"
        ),
        "cohorts_unclear": (
            "मुझे आपका जवाब समझ नहीं आया। कृपया जो लागू हो उसका नंबर "
            "या कीवर्ड भेजें (जैसे '1, 3' या 'diabetes, fall risk') "
            "— या '4' / 'none' अगर कुछ भी लागू न हो।"
        ),
        "consent_prompt": (
            "समझ गया — दर्ज किया: {picked_label}। एक आख़िरी बात — "
            "क्या मैं यहाँ दवा, अपॉइंटमेंट, रीफिल और लैब के "
            "रिमाइंडर भेज सकता हूँ? कृपया YES या NO में जवाब दें।"
        ),
        "consent_unclear": (
            "माफ़ कीजिए, समझ नहीं आया। यदि रिमाइंडर भेजना ठीक है तो "
            "YES, नहीं तो NO लिखें।"
        ),
        "complete_yes": (
            "सब तैयार! ✓ मैं ज़रूरत पड़ने पर यहाँ रिमाइंडर भेजूँगा। "
            "आप कभी भी अपॉइंटमेंट बुक करने, आने वाली विज़िट देखने, "
            "या दवा लेने का रिकॉर्ड रखने के लिए मुझे संदेश भेज "
            "सकते हैं।"
        ),
        "complete_no": (
            "ठीक है — अभी कोई रिमाइंडर नहीं। ✓ आप अपॉइंटमेंट बुक "
            "करने या सवाल पूछने के लिए कभी भी संदेश भेज सकते हैं, "
            "और 'enable reminders' लिखकर रिमाइंडर वापस चालू कर "
            "सकते हैं।"
        ),
        "label_none": "कोई नहीं",
        "escalated": (
            "माफ़ कीजिए, आपकी जानकारी समझने में दिक़्क़त हो रही है। "
            "हमारी टीम का कोई सदस्य जल्दी आपसे संपर्क करेगा और "
            "प्रोफ़ाइल पूरी करने में मदद करेगा। अभी आपको और कुछ "
            "करने की ज़रूरत नहीं है।"
        ),
    },
}


def _render(slug: str, language: str | None, **kwargs: Any) -> str:
    """Resolve a message slug for the given language. Unknown languages
    fall back to English silently — the dropdown allowlist already
    constrains what can be set, but we want to be robust to legacy
    rows or future codes added before translations land."""
    table = _MESSAGES.get((language or "en")) or _MESSAGES["en"]
    template = table.get(slug) or _MESSAGES["en"][slug]
    if kwargs:
        try:
            return template.format(**kwargs)
        except (KeyError, IndexError):
            # Mis-keyed format string falls back to English template
            # so the patient still gets *something* readable.
            return _MESSAGES["en"][slug].format(**kwargs)
    return template


# ---- Handler ---------------------------------------------------------------


def _is_stale(patient: Any, *, now: datetime | None = None) -> bool:
    """True if the patient's last onboarding transition is older than
    ``STALE_AFTER_DAYS``. NULL ``onboarding_step_at`` returns False —
    legacy rows pre-migration shouldn't get reset just because the
    column is empty."""
    when = getattr(patient, "onboarding_step_at", None)
    if when is None:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(
        days=STALE_AFTER_DAYS
    )
    return when < cutoff


async def _reprompt_or_escalate(
    db: Any,
    patient: Any,
    *,
    lang: str,
    base_slug: str,
    base_audit_code: str,
) -> dict[str, Any]:
    """Bump the retry counter and decide between standard re-prompt and
    escalation copy.

    Below ``ESCALATION_THRESHOLD``: send the standard re-prompt
    (``base_slug``). At/above threshold: open an ``onboarding_stuck``
    ops ticket if one isn't already open for this patient, and switch
    to the ``escalated`` copy. Ticket creation is idempotent — using
    ``find_open_for_patient_category`` so repeated failures never spawn
    duplicate tickets."""
    new_count = await patients_repo.bump_onboarding_retry(db, patient.id)
    if new_count is None:
        # Patient row vanished mid-flight — fall back to plain re-prompt.
        return _reply(
            _render(base_slug, lang), audit_codes=[base_audit_code]
        )

    if new_count < ESCALATION_THRESHOLD:
        return _reply(
            _render(base_slug, lang), audit_codes=[base_audit_code]
        )

    # We're at or above threshold — escalate (idempotent).
    audit = [base_audit_code, "onboarding_escalated"]
    existing = await ops_tickets_repo.find_open_for_patient_category(
        db,
        patient_id=patient.phone,
        category=ESCALATION_CATEGORY,
    )
    if existing is None:
        notes = (
            f"Patient stuck in onboarding at step "
            f"{patient.onboarding_step!r} — {new_count} consecutive "
            f"invalid replies. patient.id={patient.id}, "
            f"phone={patient.phone}."
        )
        ticket = await ops_tickets_repo.create(
            db,
            patient_id=patient.phone,
            category=ESCALATION_CATEGORY,
            priority=ESCALATION_PRIORITY,
            sla_minutes=ESCALATION_SLA_MINUTES,
            notes=notes,
        )
        log.info(
            "onboarding escalation: opened ticket %s for patient %s "
            "(step=%s, retries=%d)",
            ticket.id,
            patient.phone,
            patient.onboarding_step,
            new_count,
        )
    return _reply(_render("escalated", lang), audit_codes=audit)


async def handle_onboarding(
    *,
    patient_phone: str,
    new_user_text: str,
) -> dict[str, Any] | None:
    """Process one turn of onboarding for a patient. Returns a state delta
    for the AgentState. Returns None ONLY if the patient is already done
    (caller should fall through to the rest of the workflow)."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        patient = await patients_repo.get_by_phone(db, patient_phone)
        if patient is None:
            # Shouldn't happen — upsert_patient runs before this — but be
            # defensive: skip rather than crash.
            log.warning(
                "onboarding handler: patient %s not found", patient_phone
            )
            return None

        step = patient.onboarding_step or PENDING
        lang = getattr(patient, "preferred_language", None) or "en"

        # Stale reset: a patient who ghosted the flow long enough that
        # their context is worthless gets reset to PENDING here so the
        # rest of the handler re-greets them from scratch. We flip the
        # in-memory ``step`` so the subsequent branch matches.
        if step != DONE and _is_stale(patient):
            log.info(
                "onboarding stale reset: patient %s last advanced %s — "
                "resetting to PENDING",
                patient_phone,
                getattr(patient, "onboarding_step_at", None),
            )
            await patients_repo.update_onboarding(
                db, patient.id, step=PENDING
            )
            patient.onboarding_step = PENDING
            step = PENDING

        if step == DONE:
            return None  # caller falls through to normal routing

        if step == PENDING:
            await patients_repo.update_onboarding(
                db, patient.id, step=NEEDS_NAME
            )
            await db.commit()
            return _reply(
                _render("greeting", lang),
                audit_codes=["onboarding_greeting"],
            )

        if step == NEEDS_NAME:
            name = validate_name(new_user_text)
            if name is None:
                delta = await _reprompt_or_escalate(
                    db,
                    patient,
                    lang=lang,
                    base_slug="name_invalid",
                    base_audit_code="onboarding_name_invalid",
                )
                await db.commit()
                return delta
            await patients_repo.update_onboarding(
                db,
                patient.id,
                step=NEEDS_COHORTS,
                full_name=name,
            )
            await db.commit()
            return _reply(
                _render("cohorts_prompt", lang, name=name),
                audit_codes=["onboarding_name_captured"],
            )

        if step == NEEDS_COHORTS:
            cohorts = parse_cohorts(new_user_text or "")
            if cohorts is None:
                # Garbage / unparseable — re-prompt rather than silently
                # commit all-False as if the patient said "none".
                delta = await _reprompt_or_escalate(
                    db,
                    patient,
                    lang=lang,
                    base_slug="cohorts_unclear",
                    base_audit_code="onboarding_cohorts_unclear",
                )
                await db.commit()
                return delta
            await patients_repo.update_onboarding(
                db,
                patient.id,
                step=NEEDS_CONSENT,
                cohort_diabetes=cohorts["diabetes"],
                cohort_cardiac=cohorts["cardiac"],
                cohort_fall_risk=cohorts["fall_risk"],
            )
            await db.commit()
            picked = [k for k, v in cohorts.items() if v]
            picked_label = (
                ", ".join(picked).replace("_", " ")
                if picked
                else _render("label_none", lang)
            )
            return _reply(
                _render(
                    "consent_prompt", lang, picked_label=picked_label
                ),
                audit_codes=["onboarding_cohorts_captured"],
            )

        if step == NEEDS_CONSENT:
            consent = parse_consent(new_user_text or "")
            if consent is None:
                delta = await _reprompt_or_escalate(
                    db,
                    patient,
                    lang=lang,
                    base_slug="consent_unclear",
                    base_audit_code="onboarding_consent_unclear",
                )
                await db.commit()
                return delta
            await patients_repo.update_onboarding(
                db,
                patient.id,
                step=DONE,
                consent_sms=consent,
            )
            await db.commit()
            slug = "complete_yes" if consent else "complete_no"
            return _reply(_render(slug, lang), audit_codes=["onboarding_complete"])

        # Unknown state — log and fall through.
        log.warning(
            "onboarding handler: unknown step %r for patient %s",
            step,
            patient_phone,
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
        "quick_replies": ["CALL", "HELP"],
        "buttons": [],
        "list_rows": [],
        "list_button_label": None,
        "list_section_title": None,
    }

"""Doctor-inbox triage classifier.

Runs an LLM pass over a freeform inbound message AFTER the orchestrator
workflow has already produced a reply. The classification doesn't change
the bot's response — it's purely a read-side annotation that powers the
ops console ``/inbox`` view so a clinician can skim "what's coming in,
what slipped through".

Action-tap messages (``[dose-action] ...`` and friends) skip the LLM
call entirely and persist with a deterministic ``action_tap`` category.
"""

from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel, Field

from services.orchestrator.llm import get_llm

log = logging.getLogger(__name__)


Category = Literal[
    "clinical_question",
    "administrative",
    "billing",
    "scheduling",
    "faq",
    "social",
    "unsafe",
    "action_tap",
    "unknown",
]
Urgency = Literal["critical", "high", "medium", "low"]


class ClassifierOutput(BaseModel):
    category: Category
    urgency: Urgency = Field(
        description=(
            "Operational urgency — how soon a human needs to look. "
            "critical = within the hour, high = same day, medium = within 2 "
            "days, low = no human needed unless something else flagged it."
        )
    )
    summary: str = Field(
        max_length=200,
        description="One-line plain-language summary, ≤200 chars, no clinical advice.",
    )


class Classification(BaseModel):
    category: str
    urgency: str
    summary: str | None


_TAP_PREFIXES: tuple[str, ...] = (
    "[dose-action]",
    "[lab-action]",
    "[refill-action]",
    "[recap-action]",
    "[appt-action]",
    "[order-action]",
)


_SYSTEM_PROMPT = (
    "You triage a patient's WhatsApp message into one of nine categories so "
    "a clinician's inbox can show what's coming in. You DO NOT generate a "
    "clinical reply — that's already been done.\n\n"
    "Categories (pick exactly one):\n"
    "- clinical_question: medical, symptom, dosing, side-effect, "
    "treatment, lab/test result, pregnancy, anything that needs clinical "
    "judgement.\n"
    "- administrative: forms, paperwork, records, ID/consent updates.\n"
    "- billing: insurance, copay, costs, claims, payment.\n"
    "- scheduling: appointment booking / cancel / reschedule asks (when "
    "the bot already routes it, still log here so the inbox shows the "
    "demand).\n"
    "- faq: general program / app / how-to questions resolved by the bot.\n"
    "- social: greetings, thanks, off-topic chitchat.\n"
    "- unsafe: red-flag symptoms (chest pain, suicidal ideation, severe "
    "bleeding, anaphylaxis, stroke), explicit harm.\n"
    "- unknown: cannot tell from the message alone.\n\n"
    "Urgency:\n"
    "- critical: red-flag clinical, immediate human attention.\n"
    "- high: clinically concerning but not red-flag (new side effect, "
    "elevated BP / sugar reading, missed multiple doses).\n"
    "- medium: routine clinical question, billing dispute, scheduling "
    "frustration.\n"
    "- low: routine FAQ, social, simple admin.\n\n"
    "Summary: ≤200 chars, plain language, neutral tone, no diagnosis. "
    "Treat the summary as 'what is the patient asking / saying?', not "
    "'what should the doctor do?'.\n\n"
    "LANGUAGE: write the summary in ENGLISH regardless of the language "
    "the patient typed in. The doctor's inbox triages many patients "
    "across multiple languages — one consistent summary language is "
    "what makes the inbox scannable. The patient's verbatim words are "
    "preserved separately on the row in their original language; the "
    "summary is the doctor-facing English gloss."
)


def is_action_tap(text: str | None) -> bool:
    """Tap-routed messages have a structured marker prefix. Skip the
    LLM and persist with a deterministic ``action_tap`` category."""
    if not text:
        return False
    stripped = text.lstrip()
    return any(stripped.startswith(p) for p in _TAP_PREFIXES)


def deterministic_for_action_tap() -> Classification:
    """Constant classification for marker-prefixed inbounds — no LLM
    needed since the structured handlers already know what it is."""
    return Classification(
        category="action_tap",
        urgency="low",
        summary="Patient tapped a structured action button.",
    )


async def classify_inbound(
    *, text: str | None, intent: str | None = None
) -> Classification:
    """Classify a freeform inbound. Returns a deterministic ``unknown``
    when text is empty or the LLM call fails — caller still gets a row
    in the inbox, just without the rich annotation. Never raises."""
    if not text:
        return Classification(
            category="unknown",
            urgency="low",
            summary=None,
        )

    if is_action_tap(text):
        return deterministic_for_action_tap()

    llm = get_llm()
    if not llm.enabled:
        # Deterministic fallback when LLM is disabled (tests, no API key).
        # We still want a row so the inbox isn't empty in dev environments.
        return Classification(
            category="unknown",
            urgency="low",
            summary=text.strip()[:200] or None,
        )

    try:
        client = llm._get_client()
        if client is None:
            return Classification(
                category="unknown", urgency="low", summary=None
            )
        user_payload = (
            f"detected_intent: {intent or 'unknown'}\n"
            f"patient_message: {text.strip()[:1500]}"
        )
        from services.orchestrator.llm_tracking import track_llm_call

        async with track_llm_call(
            call_kind="classify_inbound", model=llm.model
        ) as tracker:
            completion = await client.chat.completions.parse(
                model=llm.model,
                response_format=ClassifierOutput,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_payload},
                ],
            )
            tracker.set_completion(completion)
        parsed: ClassifierOutput | None = completion.choices[0].message.parsed
        if parsed is None:
            return Classification(
                category="unknown", urgency="low", summary=None
            )
        return Classification(
            category=parsed.category,
            urgency=parsed.urgency,
            summary=parsed.summary.strip() if parsed.summary else None,
        )
    except Exception as exc:  # noqa: BLE001 — fallback semantics
        log.warning("inbox classification LLM call failed: %s", exc)
        return Classification(
            category="unknown",
            urgency="low",
            summary=text.strip()[:200] or None,
        )

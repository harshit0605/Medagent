"""Educational microcontent (G5).

Short, pre-written, non-diagnostic explainers a patient can pull on demand —
"what is HbA1c?", "how do I use a spacer?", "what's a normal blood pressure?".
Keeps the bot useful between reminders and improves health literacy without an
LLM round-trip (the content is curated + reviewed, not generated).

Each snippet is keyed by topic with trigger phrases. The handler matches an
inbound question to the best topic and returns its content. No match → None
(the caller falls through to the normal flow / LLM).

Scope: education only. Anything that reads as a symptom or a request for a
personal medical decision is NOT answered here — it falls through to triage.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Snippet:
    topic: str
    triggers: tuple[str, ...]  # lowercase substrings / phrases
    body: str
    cohorts: tuple[str, ...] = field(default_factory=tuple)  # informational


# Curated library. Bodies are intentionally short, plain, and non-prescriptive
# — they explain a concept, never tell THIS patient what to do.
_LIBRARY: tuple[Snippet, ...] = (
    Snippet(
        topic="hba1c",
        triggers=("what is hba1c", "what's hba1c", "hba1c mean", "a1c"),
        body=(
            "HbA1c is a blood test that shows your average blood sugar over "
            "the past ~3 months. It's reported as a percentage — for many "
            "people with diabetes a target is under 7%, but your care team "
            "sets the right goal for you. It's checked a few times a year."
        ),
        cohorts=("diabetes",),
    ),
    Snippet(
        topic="spacer",
        triggers=(
            "how to use a spacer",
            "use my spacer",
            "use a spacer",
            "what is a spacer",
        ),
        body=(
            "A spacer is a tube that fits on your inhaler so more medicine "
            "reaches your lungs. Shake the inhaler, fit it to the spacer, "
            "breathe out, press once, then breathe in slowly and deeply (or "
            "take 4–5 normal breaths). Wait ~30 seconds before a second puff. "
            "Rinse the spacer weekly and let it air-dry."
        ),
        cohorts=("asthma",),
    ),
    Snippet(
        topic="blood_pressure",
        triggers=(
            "normal blood pressure",
            "what is good blood pressure",
            "what's a normal bp",
            "normal bp",
        ),
        body=(
            "Blood pressure has two numbers — systolic (top) over diastolic "
            "(bottom). Around 120/80 mmHg is often considered normal, but the "
            "right target depends on your health and your care team's advice. "
            "Measure when you're rested and seated, arm supported."
        ),
        cohorts=("cardiac",),
    ),
    Snippet(
        topic="insulin_storage",
        triggers=(
            "store my insulin",
            "insulin storage",
            "keep insulin",
            "store insulin",
        ),
        body=(
            "Unopened insulin keeps in the fridge (2–8°C) until its expiry. "
            "The pen/vial you're using can usually stay at room temperature "
            "(below ~30°C, out of direct sun) for up to ~28 days — check your "
            "specific product. Never freeze insulin, and don't use it if it "
            "looks cloudy when it shouldn't be."
        ),
        cohorts=("diabetes",),
    ),
    Snippet(
        topic="missed_dose",
        # Question framings only — a bare "I missed a dose" is an adherence
        # report (handled elsewhere), not an education pull.
        triggers=(
            "what if i miss",
            "what happens if i miss",
            "what should i do if i miss",
            "what if i forget",
        ),
        body=(
            "If you miss a dose, take it as soon as you remember — UNLESS it's "
            "almost time for the next one, in which case skip the missed one "
            "and continue as normal. Never double up to catch up. If you're "
            "unsure for a specific medicine, reply HELP and we'll check with "
            "your care team."
        ),
    ),
)


# A question framing makes a topic match an EDUCATIONAL pull rather than a
# symptom report. We require a topic trigger AND (optionally) a question shape.
_QUESTION_RE = re.compile(
    r"\b(what|what's|whats|how|why|when|tell me about|explain|is\b|are\b)\b",
    re.IGNORECASE,
)


def match_snippet(text: str | None) -> Snippet | None:
    """Return the best educational snippet for ``text``, or None.

    A snippet matches when one of its trigger phrases is a substring of the
    (lowercased) text. The longest matching trigger wins so a more specific
    topic beats a generic one."""
    if not text:
        return None
    low = text.lower()
    best: tuple[int, Snippet] | None = None
    for snip in _LIBRARY:
        for trig in snip.triggers:
            if trig in low and (best is None or len(trig) > best[0]):
                best = (len(trig), snip)
    return best[1] if best else None


def looks_like_education_query(text: str | None) -> bool:
    """True when the message is an educational question we can answer from the
    library. Requires a topic match; a question word strengthens it but a bare
    topic phrase ("hba1c?") also counts."""
    return match_snippet(text) is not None


async def handle_education_query(
    *, patient_phone: str, new_user_text: str
) -> dict | None:
    """Answer an educational question from the curated library. Returns a
    delta or None. No DB needed — content is static."""
    snip = match_snippet(new_user_text)
    if snip is None:
        return None
    log.info("education snippet served: %s", snip.topic)
    body = snip.body + "\n\nThis is general info, not personal medical advice — reply HELP for your care team."
    return {
        "response_body": body,
        "audit_reasons": [f"education_{snip.topic}"],
    }

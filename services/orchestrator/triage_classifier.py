"""Clinical urgency triage on inbound messages.

Layered ON TOP of the existing inbox classifier. The inbox
classifier produces a category + operational urgency for the
ops queue ("does a human need to look soon?"). This module
asks a stricter question: "is the patient telling us they're
in clinical danger right now?".

Output severity:

    none      - the message has no clinical safety content.
                Default for greetings, scheduling, FAQ, etc.
    low       - mild symptom mention, no red-flag content.
    medium    - meaningful symptom progression but no immediate
                danger signs.
    high      - patient describes symptoms that warrant same-day
                clinical attention (worsening chest tightness on
                exertion, persistent severe headache, vomiting +
                fever, rash spreading, intent to self-harm
                without immediate plan).
    critical  - immediate emergency cues (active chest pain,
                stroke signs, anaphylaxis, severe bleeding,
                suicidal intent with plan, anaphylactic
                breathing distress, infant unresponsiveness).

Why a separate LLM call instead of folding into the existing
classifier? Three reasons:

    1. Different rubric. The inbox classifier optimises for
       category fidelity ("is this billing or scheduling?").
       Triage optimises for safety recall — false positives are
       cheap (doctor takes 30s to mark "non-urgent"); false
       negatives are catastrophic.
    2. Different model possible. We may want a more
       conservative model here, or one with longer context for
       prior-history awareness.
    3. Independent latency budget. The /route response can ship
       BEFORE this classification finishes (best-effort persist),
       same as inbox classification.

Returns ``TriageDecision``. The caller (orchestrator /route)
persists a ``ClinicalAlert`` row when severity ∈ {high, critical}.
"""

from __future__ import annotations

import logging
import os
from typing import Literal

from pydantic import BaseModel, Field

from services.orchestrator.llm import get_llm

log = logging.getLogger(__name__)


def _triage_enabled() -> bool:
    """Kill switch for the triage classifier.

    Read at every call (NOT cached at module-import time) so
    operators can flip the env var on a running process and
    have the next /route invocation honour it without a
    restart. The eventual cost spike of always-reading is
    negligible vs the latency of getting an LLM ban-hammered
    into a deploy roll.

    Default ON when the env var is absent — V1 ships with the
    classifier active. To disable: ``TRIAGE_ENABLED=0``.
    """
    return os.getenv("TRIAGE_ENABLED", "1") != "0"


Severity = Literal["none", "low", "medium", "high", "critical"]


# Severities at or above this threshold create an alert row.
# Kept explicit so the orchestrator hook can branch off the same
# constant the classifier knows about.
ALERT_SEVERITIES: frozenset[str] = frozenset({"high", "critical"})


class TriageDecision(BaseModel):
    """Structured triage output. ``red_flags`` lists the
    specific triggers the model identified (free-form short
    strings — "chest pain", "vomiting blood"). When severity
    is ``none`` or ``low`` the list is typically empty."""

    severity: Severity
    red_flags: list[str] = Field(default_factory=list)
    summary: str = Field(
        max_length=300,
        description=(
            "1-line plain-language description of the clinical "
            "concern, written for a doctor reading a queue."
        ),
    )


_SYSTEM_PROMPT = (
    "You triage a patient's WhatsApp message for clinical "
    "URGENCY. You are NOT diagnosing or treating; you are "
    "deciding whether the message contains content that "
    "requires same-day or immediate clinician attention.\n\n"
    "Severity scale (pick exactly one):\n"
    "- none: greetings, scheduling, billing, paperwork, "
    "neutral updates, FAQ.\n"
    "- low: mild symptom mention, easily self-managed, no "
    "progression cues. Examples: 'slight headache today', "
    "'mild nausea after the new pill'.\n"
    "- medium: meaningful symptom but not red-flag. Examples: "
    "'side effects getting worse', 'rash appeared on arms', "
    "'sleep has been bad for a week'.\n"
    "- high: same-day clinical attention warranted. Examples: "
    "'chest tightness when I walk upstairs', 'persistent "
    "vomiting since yesterday', 'rash spreading and itching "
    "badly', 'thinking about hurting myself but no plan'.\n"
    "- critical: red-flag emergency cues. Examples: 'chest "
    "pain right now', 'severe headache and confusion', "
    "'stroke face / arm / speech symptoms', 'severe allergic "
    "reaction breathing trouble', 'suicidal intent with "
    "plan', 'severe bleeding', 'unresponsive infant', "
    "'anaphylaxis'.\n\n"
    "Hard rules:\n"
    "- err toward HIGHER severity when ambiguous. False "
    "positives are cheap; false negatives are catastrophic.\n"
    "- red_flags is a list of short strings naming the "
    "specific triggers you identified. Empty list when severity "
    "is none/low and no triggers fired.\n"
    "- summary is ≤300 chars, plain language, written for a "
    "doctor scanning a queue. No clinical advice. No diagnosis.\n"
    "- The patient may write in any language. Triage on "
    "meaning, not literal English keywords.\n"
    "Output JSON with three fields: severity, red_flags, summary."
)


async def classify_clinical_urgency(
    *, text: str, intent: str | None = None
) -> TriageDecision:
    """Classify ``text`` for clinical urgency. Best-effort:
    on any LLM failure (disabled, network, parse error) we
    return a ``none``-severity decision so the caller's
    persistence path treats this as "no alert needed". The
    intent classifier still runs — we don't want a triage
    failure to drop the inbox row.

    ``intent`` is the inbox classifier's intent string; we
    pass it as soft context so the model can disambiguate
    (e.g. "rescheduling — not relevant to clinical urgency")
    but we don't trust it as authoritative.
    """
    if not text or not text.strip():
        return TriageDecision(severity="none", summary="(empty message)")

    # Kill switch — when ops flips ``TRIAGE_ENABLED=0`` the
    # classifier short-circuits to ``none``. The /route hook
    # treats none as "no alert", so inbound flows continue
    # normally without burning LLM tokens or filling the
    # alerts queue. Distinct from ``LLM_ENABLED`` which kills
    # ALL LLM calls; this is scoped to triage only.
    if not _triage_enabled():
        return TriageDecision(
            severity="none",
            summary="(triage disabled — TRIAGE_ENABLED=0)",
        )

    llm = get_llm()
    if not llm.enabled:
        return TriageDecision(
            severity="none", summary="(LLM disabled — triage skipped)"
        )

    try:
        client = llm._get_client()
        if client is None:
            return TriageDecision(
                severity="none",
                summary="(LLM client unavailable — triage skipped)",
            )

        from services.orchestrator.llm_tracking import track_llm_call

        user_blob = f"intent: {intent or 'unknown'}\n\nmessage:\n{text}"

        async with track_llm_call(
            call_kind="clinical_triage", model=llm.model
        ) as tracker:
            completion = await client.chat.completions.parse(
                model=llm.model,
                response_format=TriageDecision,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_blob},
                ],
            )
            tracker.set_completion(completion)

        parsed: TriageDecision | None = (
            completion.choices[0].message.parsed
        )
        if parsed is None:
            log.warning("triage classifier returned no parsed body")
            return TriageDecision(
                severity="none", summary="(LLM parse failed)"
            )
        return parsed

    except Exception as exc:  # noqa: BLE001 — best-effort
        log.warning("clinical triage failed: %s", exc)
        return TriageDecision(
            severity="none",
            summary=f"(triage failed: {type(exc).__name__})",
        )

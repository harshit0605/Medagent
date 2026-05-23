"""LLM-drafted doctor reply suggestions.

When ops opens an inbox row that needs a manual reply, instead
of typing from a blank textarea they get a context-aware draft
the bot pre-filled. Doctor (or ops) reviews, edits, sends —
the same ``sendDoctorReply`` path as before. The draft never
goes to the patient unless someone clicks Send.

Inputs gathered (read-only, no new tables):

    1. Slice-7 timeline events for the last N days (messages,
       doses, side-effects, alerts) — the "what's been
       happening with this patient" context.
    2. Active regimens — so the draft doesn't suggest stopping
       a med the patient isn't on, etc.
    3. Open clinical alerts — heightens caveats when relevant.
    4. The verbatim inbound message that needs answering.

Output schema:

    draft_text   - the proposed reply (≤500 chars)
    confidence   - "high" | "medium" | "low" — the LLM's own
                   read on whether the draft is safe to send
                   without heavy edits. Low confidence triggers
                   a UI warning.
    caveats      - list of short strings flagging things the
                   reviewer should consider before sending
                   ("verify with prescribing doctor",
                   "patient hasn't responded to last 3
                   reminders").

Failure path:
    LLM disabled / failed → returns ``DraftReply`` with empty
    draft_text and confidence="low" plus an error caveat. The
    UI surfaces this gracefully ("AI draft unavailable — type
    manually") rather than blocking the textarea.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import (
    clinical_alerts as clinical_alerts_repo,
    regimens as regimens_repo,
)
from services.orchestrator import patient_timeline
from services.orchestrator.llm import get_llm

log = logging.getLogger(__name__)


MAX_DRAFT_CHARS = 500
MAX_CAVEATS = 4


class DraftReply(BaseModel):
    draft_text: str
    confidence: Literal["high", "medium", "low"]
    caveats: list[str] = Field(default_factory=list)
    llm_model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class _LLMDraftBody(BaseModel):
    """Structured LLM output. Confidence + caveats are
    self-reported by the model and treated as advisory — the
    reviewer always has the final call."""

    draft_text: str = Field(
        description=(
            "The proposed reply. Plain-text, ≤500 chars, no "
            "markdown / emoji / headers, written as if the "
            "doctor is speaking."
        )
    )
    confidence: Literal["high", "medium", "low"] = Field(
        description=(
            "Self-rated confidence the draft is safe to send "
            "without heavy edits. ``low`` = many caveats, "
            "high uncertainty."
        )
    )
    caveats: list[str] = Field(
        default_factory=list,
        description=(
            "Short bullets flagging things the reviewer "
            "should consider before sending. Empty list when "
            "nothing meaningful to flag."
        ),
    )


_SYSTEM_PROMPT = (
    "You draft a SHORT reply that a doctor or ops reviewer "
    "will then approve and send to a patient on WhatsApp. You "
    "are NOT replacing clinical judgement; you are reducing "
    "typing time for routine acknowledgements + reassurance. "
    "The reviewer ALWAYS edits / approves before send.\n\n"
    "Hard rules:\n"
    "- Plain text, ≤500 chars, no markdown, no emoji, no "
    "section headers.\n"
    "- Address the patient by first name when available.\n"
    "- Acknowledge what they said before answering.\n"
    "- Stay within scope: dosing/medication advice OK only "
    "if it matches the active regimens listed. Never invent "
    "diagnoses, dosages, or instructions not authorised by "
    "the doctor.\n"
    "- If unsure or if a clinical alert is open, set "
    "confidence='low' and list explicit caveats.\n"
    "- For pure pleasantries / scheduling / FAQ, confidence "
    "can be 'high'.\n"
    "- Caveats are short imperatives the reviewer should "
    "verify ('confirm with prescribing doctor', 'patient "
    "has open critical alert'). Empty list is fine when no "
    "meaningful caveats apply.\n"
    "Output JSON: draft_text, confidence, caveats."
)


def _serialise_timeline(
    events: list[patient_timeline.TimelineEvent],
) -> str:
    if not events:
        return "(no recent activity)"
    lines: list[str] = []
    for e in events[:40]:  # cap context tokens
        line = f"[{e.occurred_at.isoformat()}] {e.kind}: {e.title}"
        if e.body:
            line = f"{line} :: {e.body[:120]}"
        lines.append(line)
    return "\n".join(lines)


def _serialise_regimens(regimens: list) -> str:
    if not regimens:
        return "(no active regimens)"
    return "\n".join(
        f"- {r.medication_name} {r.dose}" for r in regimens
    )


def _serialise_alerts(alerts: list) -> str:
    open_alerts = [a for a in alerts if a.status == "open"]
    if not open_alerts:
        return "(no open clinical alerts)"
    return "\n".join(
        f"- {a.severity}: {a.clinical_summary}"
        for a in open_alerts[:5]
    )


def _truncate_caveats(items: list[str]) -> list[str]:
    out: list[str] = []
    for s in items[:MAX_CAVEATS]:
        s = s.strip()
        if not s:
            continue
        if len(s) > 140:
            s = s[:139].rstrip() + "…"
        out.append(s)
    return out


async def draft_reply(
    session: AsyncSession,
    *,
    patient_id: int,
    inbound_text: str,
    patient_first_name: str | None = None,
    context_days: int = 7,
) -> DraftReply:
    """Build a draft reply for ``inbound_text`` against this
    patient's recent context. Always returns a ``DraftReply``
    — failures degrade to empty draft + low confidence rather
    than raising, so the UI can render a graceful "type
    manually" fallback."""
    if not inbound_text or not inbound_text.strip():
        return DraftReply(
            draft_text="",
            confidence="low",
            caveats=["empty inbound message — nothing to reply to"],
        )

    until = datetime.now(timezone.utc) + timedelta(seconds=60)
    since = until - timedelta(days=context_days)

    # Best-effort context gather — each source independently
    # falls back to empty / placeholder so a partial failure
    # doesn't block the draft.
    try:
        events = await patient_timeline.build_timeline(
            session,
            patient_db_id=patient_id,
            since=since,
            until=until,
            limit=80,
        )
    except Exception:  # noqa: BLE001 — non-fatal
        log.exception("draft_reply timeline gather failed")
        events = []

    try:
        regimens = await regimens_repo.list_for_patient(
            session, patient_id
        )
    except Exception:  # noqa: BLE001
        log.exception("draft_reply regimen gather failed")
        regimens = []

    try:
        alerts = await clinical_alerts_repo.list_for_patient(
            session, patient_id, limit=5
        )
    except Exception:  # noqa: BLE001
        log.exception("draft_reply alert gather failed")
        alerts = []

    llm = get_llm()
    model_name = getattr(llm, "model", "stub")

    if not llm.enabled:
        return DraftReply(
            draft_text="",
            confidence="low",
            caveats=[
                "AI draft unavailable (LLM disabled) — type manually",
            ],
            llm_model=model_name,
        )

    user_payload = (
        f"patient_first_name: {patient_first_name or '—'}\n\n"
        f"active_regimens:\n{_serialise_regimens(regimens)}\n\n"
        f"open_clinical_alerts:\n{_serialise_alerts(alerts)}\n\n"
        f"recent_timeline (last {context_days} days):\n"
        f"{_serialise_timeline(events)}\n\n"
        f"inbound_message:\n{inbound_text}\n\n"
        "Draft a reply now."
    )

    try:
        client = llm._get_client()
        if client is None:
            raise RuntimeError("LLM client unavailable")

        from services.orchestrator.llm_tracking import track_llm_call

        async with track_llm_call(
            call_kind="doctor_reply_draft", model=model_name
        ) as tracker:
            completion = await client.chat.completions.parse(
                model=model_name,
                response_format=_LLMDraftBody,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_payload},
                ],
            )
            tracker.set_completion(completion)

        parsed: _LLMDraftBody | None = (
            completion.choices[0].message.parsed
        )
        if parsed is None:
            raise RuntimeError("LLM returned no parsed body")

        usage = getattr(completion, "usage", None)
        prompt_tokens = (
            getattr(usage, "prompt_tokens", None)
            if usage is not None
            else None
        )
        completion_tokens = (
            getattr(usage, "completion_tokens", None)
            if usage is not None
            else None
        )

        text = (parsed.draft_text or "").strip()[:MAX_DRAFT_CHARS]

        # Auto-add a caveat when the patient has an open
        # critical alert — even if the LLM didn't catch it,
        # we want the reviewer to know. Belt-and-suspenders.
        caveats = list(parsed.caveats or [])
        has_critical = any(
            a.status == "open" and a.severity == "critical"
            for a in alerts
        )
        if has_critical and not any(
            "critical" in c.lower() and "alert" in c.lower()
            for c in caveats
        ):
            caveats.append(
                "Patient has an open critical clinical alert — "
                "review on /clinical-alerts before sending."
            )
        caveats = _truncate_caveats(caveats)

        # If a critical alert exists, force confidence to
        # ``low`` regardless of what the LLM self-rated. The
        # reviewer must consciously confirm.
        confidence: Literal["high", "medium", "low"] = (
            "low" if has_critical else parsed.confidence
        )

        return DraftReply(
            draft_text=text,
            confidence=confidence,
            caveats=caveats,
            llm_model=model_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    except Exception as exc:  # noqa: BLE001 — degrade gracefully
        log.warning("doctor reply draft failed: %s", exc)
        return DraftReply(
            draft_text="",
            confidence="low",
            caveats=[
                f"AI draft failed ({type(exc).__name__}) — type manually",
            ],
            llm_model=model_name,
        )

"""Generator for after-visit patient recap WhatsApp messages.

The doctor authors structured fields (meds added/changed/stopped, labs
ordered, follow-up plan, red flags) plus optional free-text notes in the
ops console. This module turns that bundle into a friendly, plain-text
WhatsApp message — short enough to fit comfortably in one bubble (~600
chars), with bullet sections the patient can re-read later.

Two paths:
- LLM-driven (preferred when ``LLM_ENABLED=1``): polishes the structured
  payload + free-text into a human-friendly recap. Falls back if the
  LLM call fails.
- Deterministic fallback: a template renderer that builds the same
  structure without LLM. Used when the LLM is disabled, fails, or in
  tests. Kept in lock-step shape with the LLM output so QA expectations
  don't fork.

Hard limits:
- ≤900 chars (covers a 4-section recap comfortably; WhatsApp bubble
  splits become ugly past ~1024).
- No diagnostic claims, no medication advice the doctor didn't already
  authorize. The patient sees only what the doctor approved.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from services.orchestrator.llm import get_llm


log = logging.getLogger(__name__)


# Hard cap on the rendered message. WhatsApp will accept much more, but
# longer messages are noticeably less likely to be re-read.
MAX_RECAP_CHARS = 900


class RecapContext(BaseModel):
    """Inputs the renderer needs to produce a recap."""

    patient_first_name: str | None = None
    doctor_name: str
    appointment_date_local: str  # already formatted in patient's TZ
    doctor_notes: str | None = None
    meds_added: list[dict[str, Any]] = Field(default_factory=list)
    meds_changed: list[dict[str, Any]] = Field(default_factory=list)
    meds_stopped: list[dict[str, Any]] = Field(default_factory=list)
    labs_ordered: list[dict[str, Any]] = Field(default_factory=list)
    next_followup_in_days: int | None = None
    red_flags: list[str] = Field(default_factory=list)
    # ISO-639-1 from the patient's profile. ``None`` and ``en`` both
    # produce English. The deterministic fallback is English-only by
    # design; the LLM path is the multilingual surface.
    preferred_language: str | None = None


class _RecapBody(BaseModel):
    body: str


_RECAP_SYSTEM_PROMPT = (
    "You write a concise after-visit recap that a patient receives on WhatsApp. Hard rules:\n"
    "- Plain text only. No markdown, no emoji, no headers like '##'.\n"
    "- ≤900 characters total.\n"
    "- Do NOT add medical advice, dosages, or diagnoses the doctor did not authorize.\n"
    "- Use simple language understandable at an 8th-grade reading level.\n"
    "- Structure the message in the order: greeting, what we discussed, medications, "
    "tests, next visit, red-flag warnings, sign-off.\n"
    "- For medications, render each entry on its own line prefixed with '• '.\n"
    "- Skip any section that has no content (don't write 'No medications').\n"
    "- For red flags, prefix the section with: 'Call us right away if:'.\n"
    "- End with: 'Reply OK to acknowledge, or QUESTION if anything is unclear.'\n"
    "Output JSON with a single 'body' field containing the message."
)


def _render_med_line(entry: dict[str, Any]) -> str:
    name = (entry.get("name") or entry.get("medication_name") or "").strip()
    instructions = (entry.get("instructions") or entry.get("change") or "").strip()
    if instructions:
        return f"• {name} — {instructions}"
    return f"• {name}"


def render_deterministic(ctx: RecapContext) -> str:
    """Template-based renderer used as the LLM fallback. Kept simple and
    structurally identical to what the LLM is told to produce."""
    lines: list[str] = []

    greeting = (
        f"Hi {ctx.patient_first_name}, "
        if ctx.patient_first_name
        else "Hi, "
    )
    lines.append(
        f"{greeting}here's a recap of your visit with {ctx.doctor_name} on "
        f"{ctx.appointment_date_local}."
    )

    if ctx.doctor_notes:
        lines.append("")
        lines.append(ctx.doctor_notes.strip())

    if ctx.meds_added or ctx.meds_changed or ctx.meds_stopped:
        lines.append("")
        lines.append("Medications:")
        for m in ctx.meds_added:
            lines.append(f"{_render_med_line(m)} (new)")
        for m in ctx.meds_changed:
            lines.append(f"{_render_med_line(m)} (updated)")
        for m in ctx.meds_stopped:
            name = (m.get("name") or m.get("medication_name") or "").strip()
            lines.append(f"• Stop taking {name}")

    if ctx.labs_ordered:
        lines.append("")
        lines.append("Tests to do:")
        for lab in ctx.labs_ordered:
            test_name = (lab.get("test_name") or "").strip()
            lines.append(f"• {test_name}")

    if ctx.next_followup_in_days is not None and ctx.next_followup_in_days > 0:
        lines.append("")
        if ctx.next_followup_in_days == 1:
            when = "tomorrow"
        elif ctx.next_followup_in_days < 14:
            when = f"in {ctx.next_followup_in_days} days"
        elif ctx.next_followup_in_days < 60:
            weeks = round(ctx.next_followup_in_days / 7)
            when = f"in about {weeks} weeks"
        else:
            months = round(ctx.next_followup_in_days / 30)
            when = f"in about {months} months"
        lines.append(f"Next visit: {when}.")

    if ctx.red_flags:
        lines.append("")
        lines.append("Call us right away if:")
        for flag in ctx.red_flags:
            lines.append(f"• {flag.strip()}")

    lines.append("")
    lines.append("Reply OK to acknowledge, or QUESTION if anything is unclear.")

    body = "\n".join(line for line in lines if line is not None)
    return body[:MAX_RECAP_CHARS]


def _render_user_payload(ctx: RecapContext) -> str:
    """Compact JSON-ish payload for the LLM to expand into the recap."""
    parts: list[str] = []
    if ctx.patient_first_name:
        parts.append(f"patient_first_name: {ctx.patient_first_name}")
    parts.append(f"doctor: {ctx.doctor_name}")
    parts.append(f"appointment_date: {ctx.appointment_date_local}")
    if ctx.doctor_notes:
        parts.append(f"doctor_notes: {ctx.doctor_notes.strip()}")
    if ctx.meds_added:
        parts.append(f"meds_added: {ctx.meds_added}")
    if ctx.meds_changed:
        parts.append(f"meds_changed: {ctx.meds_changed}")
    if ctx.meds_stopped:
        parts.append(f"meds_stopped: {ctx.meds_stopped}")
    if ctx.labs_ordered:
        parts.append(f"labs_ordered: {ctx.labs_ordered}")
    if ctx.next_followup_in_days is not None:
        parts.append(f"next_followup_in_days: {ctx.next_followup_in_days}")
    if ctx.red_flags:
        parts.append(f"red_flags: {ctx.red_flags}")
    return "\n".join(parts)


async def generate_recap(ctx: RecapContext) -> str:
    """Produce the patient-facing recap message text.

    Tries the LLM first; on any failure, falls back to the deterministic
    renderer. Always returns a string (never None) so the caller can ship
    a recap regardless of LLM availability.

    When ``ctx.preferred_language`` is non-English the LLM is asked to
    produce the recap in the patient's language + native script. The
    deterministic fallback stays English-only — translating the
    template clauses without an LLM would risk clinical-tone errors.
    """
    from app import i18n

    llm = get_llm()
    if not llm.enabled:
        return render_deterministic(ctx)

    try:
        client = llm._get_client()
        if client is None:
            return render_deterministic(ctx)
        messages: list[dict[str, str]] = [
            {"role": "system", "content": _RECAP_SYSTEM_PROMPT}
        ]
        if (
            ctx.preferred_language
            and ctx.preferred_language != i18n.DEFAULT_LANGUAGE_CODE
        ):
            language_hint = i18n.llm_hint(ctx.preferred_language)
            messages.append(
                {
                    "role": "system",
                    "content": (
                        f"Write the recap in {language_hint}. Use the "
                        "native script — do NOT transliterate to Roman. "
                        "Keep the structural rules above (sections, "
                        "bullets, length cap, no medical advice the "
                        "doctor didn't authorize). The closing 'Reply OK "
                        "to acknowledge, or QUESTION if anything is "
                        "unclear.' line is part of the protocol — keep "
                        "the OK / QUESTION cue words verbatim so the "
                        "inbound handler still recognises them."
                    ),
                }
            )
        messages.append(
            {"role": "user", "content": _render_user_payload(ctx)}
        )
        from services.orchestrator.llm_tracking import track_llm_call

        async with track_llm_call(
            call_kind="recap_generate", model=llm.model
        ) as tracker:
            completion = await client.chat.completions.parse(
                model=llm.model,
                response_format=_RecapBody,
                messages=messages,
            )
            tracker.set_completion(completion)
        parsed: _RecapBody | None = completion.choices[0].message.parsed
        if parsed is None or not parsed.body.strip():
            return render_deterministic(ctx)
        body = parsed.body.strip()
        return body[:MAX_RECAP_CHARS]
    except Exception as exc:  # noqa: BLE001 — fallback semantics
        log.warning("recap LLM generation failed, using deterministic: %s", exc)
        return render_deterministic(ctx)

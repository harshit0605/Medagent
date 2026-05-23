"""LLM-powered pre-visit brief generation.

When a doctor opens a patient (or 2 hours before an
appointment fires), this generates a 30-second-read summary
of "what's happened with this patient since you last looked".

Inputs (gathered from existing tables, no new ones):

    1. Slice-7 timeline events for the last 30 days (messages,
       doses, side-effects, appointments, labs).
    2. Patient's active regimens — context for "is metformin
       still on the plan?" interpretations.
    3. Adherence summary numbers — feeds the at-a-glance
       metrics tile so the doctor doesn't have to read prose
       to learn the rate.

Outputs:

    summary           — 2-3 sentence paragraph
    talking_points    — list[str], short bullets
    red_flags         — list[str], safety concerns
    key_metrics       — adherence_rate, missed_doses,
                        side_effect_count, messages_count

Failure path:
    If the LLM call fails (network, quota, parse error) we
    record a ``failed`` brief row with the error so the
    operator can see "we tried, the LLM 500'd" rather than
    silent failure. The endpoint returns a 502 in that case so
    callers can retry.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import VisitBrief
from app.db.repositories import (
    appointments as appointments_repo,
    visit_briefs as visit_briefs_repo,
)
from services.orchestrator import patient_timeline
from services.orchestrator.llm import get_llm

log = logging.getLogger(__name__)


# Hard cap on summary prose so the doctor view doesn't grow
# unbounded. Talking points + red flags are list-shaped so the
# UI can clamp them visually.
MAX_SUMMARY_CHARS = 600
MAX_TALKING_POINTS = 6
MAX_RED_FLAGS = 4


class _BriefBody(BaseModel):
    """Structured LLM output. The OpenAI parsed completion
    materialises this directly so we don't have to do schema
    validation by hand."""

    summary: str = Field(
        description=(
            "2-3 sentence summary of the patient's last 30 "
            "days. No medical advice. Reference specific "
            "events from the timeline."
        )
    )
    talking_points: list[str] = Field(
        default_factory=list,
        description=(
            "Up to 6 short bullets the doctor should surface "
            "during the visit. Each bullet is 1 sentence."
        ),
    )
    red_flags: list[str] = Field(
        default_factory=list,
        description=(
            "Safety concerns that need explicit follow-up. "
            "Empty list when nothing meets the bar."
        ),
    )


_SYSTEM_PROMPT = (
    "You are a clinical AI assistant. You read a patient's last 30 "
    "days of digital interactions and produce a pre-visit brief for "
    "their doctor.\n\n"
    "Hard rules:\n"
    "- Output JSON with three fields: summary, talking_points, red_flags.\n"
    "- summary: 2-3 sentences, plain prose, ≤600 chars. Reference "
    "concrete events from the input — not generic statements.\n"
    "- talking_points: list of up to 6 strings; each ≤140 chars; "
    "phrased as actionable items the doctor can raise.\n"
    "- red_flags: list of up to 4 strings; only include items that "
    "warrant explicit safety follow-up (severe symptoms, repeated "
    "missed doses of high-stakes meds, abnormal lab results, sudden "
    "drop in engagement). Use an empty list when nothing meets the "
    "bar — do NOT manufacture concerns.\n"
    "- Never give medication advice or dosage changes.\n"
    "- Never include the patient's full name or phone in the output.\n"
    "- If the input is sparse (few events), say so honestly in the "
    "summary rather than inventing detail.\n"
    "- Keep tone neutral-clinical, not breezy."
)


def _serialise_timeline(
    events: list[patient_timeline.TimelineEvent],
) -> str:
    """Compact representation of timeline events for the LLM
    prompt. We don't dump the raw JSON — that wastes tokens on
    irrelevant fields. One line per event:
        ``[ISO]  KIND  TITLE  ::  body``
    Body is omitted when empty."""
    if not events:
        return "(no events in window)"
    lines: list[str] = []
    for e in events:
        line = f"[{e.occurred_at.isoformat()}]  {e.kind}  {e.title}"
        if e.body:
            line = f"{line}  ::  {e.body}"
        lines.append(line)
    return "\n".join(lines)


def _serialise_regimens(regimens: list[Any]) -> str:
    if not regimens:
        return "(no active regimens)"
    return "\n".join(
        f"- {r.medication_name} {r.dose}" for r in regimens
    )


def _compute_key_metrics(
    events: list[patient_timeline.TimelineEvent],
) -> dict[str, Any]:
    """Pre-compute the at-a-glance numbers from the timeline
    events. Centralising this in code (vs asking the LLM)
    keeps the numbers exact and deterministic — the LLM
    sometimes paraphrases counts, which is bad for a metric
    panel."""
    dose_taken = sum(1 for e in events if e.kind == "dose_taken")
    dose_missed = sum(1 for e in events if e.kind == "dose_missed")
    dose_skipped = sum(1 for e in events if e.kind == "dose_skipped")
    dose_delayed = sum(1 for e in events if e.kind == "dose_delayed")
    dose_total = (
        dose_taken + dose_missed + dose_skipped + dose_delayed
    )
    adherence_rate: float | None
    if dose_total > 0:
        adherence_rate = round(dose_taken / dose_total, 3)
    else:
        adherence_rate = None
    side_effect_count = sum(
        1 for e in events if e.kind == "side_effect"
    )
    messages_inbound = sum(
        1 for e in events if e.kind == "message_inbound"
    )
    messages_outbound = sum(
        1 for e in events if e.kind == "message_outbound"
    )
    return {
        "adherence_rate": adherence_rate,
        "doses_taken": dose_taken,
        "doses_missed": dose_missed,
        "doses_total": dose_total,
        "side_effect_count": side_effect_count,
        "messages_inbound": messages_inbound,
        "messages_outbound": messages_outbound,
    }


def _truncate_list(items: list[str], *, cap: int, max_chars: int) -> list[str]:
    out: list[str] = []
    for s in items[:cap]:
        s = s.strip()
        if not s:
            continue
        if len(s) > max_chars:
            s = s[: max_chars - 1].rstrip() + "…"
        out.append(s)
    return out


async def generate_brief(
    session: AsyncSession,
    *,
    patient_id: int,
    appointment_id: int | None = None,
    doctor_id: int | None = None,
    window_days: int = 30,
    generated_by: str | None = None,
) -> VisitBrief:
    """Build a brief from the patient's last ``window_days`` of
    activity, persist it, and return the row.

    Raises ``ValueError`` for missing patient (mapped to 404 by
    the endpoint) or invalid window_days. LLM failures are
    captured as a ``failed`` brief row + re-raised as
    ``RuntimeError`` so the caller can retry without losing
    the audit trail.
    """
    if window_days <= 0 or window_days > 180:
        raise ValueError(
            f"window_days must be in (0, 180]; got {window_days}"
        )

    # Small forward buffer to absorb clock skew between this
    # process and Postgres — server-default ``created_at``
    # values stamped by ``func.now()`` can be a few hundred ms
    # ahead of our Python clock on remote-DB deployments
    # (Supabase pgbouncer round-trip), causing fresh events to
    # be excluded from the window. 60s is generous and doesn't
    # affect doctor-facing semantics (the brief is over 30
    # days; an extra minute on the leading edge is invisible).
    until = datetime.now(timezone.utc) + timedelta(seconds=60)
    since = until - timedelta(days=window_days)

    # Reuse the slice-7 aggregator so the brief sees exactly
    # what the doctor sees in the timeline UI. If we re-queried
    # from scratch here, the brief and the timeline could
    # diverge on edge cases (which would be confusing).
    events = await patient_timeline.build_timeline(
        session,
        patient_db_id=patient_id,
        since=since,
        until=until,
        limit=300,
    )

    # Active regimens — small denormalised pull. The patient
    # detail page already loads these so the cost is amortised
    # in production; in isolation we eat one extra query.
    from app.db.repositories import regimens as regimens_repo

    regimens = await regimens_repo.list_for_patient(
        session, patient_id
    )

    key_metrics = _compute_key_metrics(events)

    # If a doctor wasn't passed, try to pick one from the
    # appointment so the brief is still attributed correctly
    # for the audit log. None is a valid fallback.
    if doctor_id is None and appointment_id is not None:
        appt = await appointments_repo.get(session, appointment_id)
        if appt is not None:
            doctor_id = appt.doctor_id

    llm = get_llm()
    model_name = getattr(llm, "model", "stub")

    if not llm.enabled:
        # Disabled-LLM environments (CI, dev without keys) get
        # a deterministic placeholder. Better than failing
        # outright — the doctor sees "(LLM disabled)" prose +
        # the metrics tile, which is still useful.
        summary = (
            "LLM disabled — automatic summary unavailable. "
            f"Reviewed window: {since.date().isoformat()} → "
            f"{until.date().isoformat()}. See the timeline for "
            "raw events."
        )
        brief = await visit_briefs_repo.create(
            session,
            patient_id=patient_id,
            appointment_id=appointment_id,
            doctor_id=doctor_id,
            window_start=since,
            window_end=until,
            llm_model=model_name,
            prompt_tokens=None,
            completion_tokens=None,
            summary=summary,
            talking_points=[],
            red_flags=[],
            key_metrics=key_metrics,
            generated_by=generated_by,
        )
        if appointment_id is not None:
            await visit_briefs_repo.supersede_for_appointment(
                session,
                appointment_id=appointment_id,
                keep_brief_id=brief.id,
            )
        return brief

    timeline_blob = _serialise_timeline(events)
    regimens_blob = _serialise_regimens(regimens)

    user_payload = (
        f"window_start: {since.isoformat()}\n"
        f"window_end: {until.isoformat()}\n\n"
        f"active_regimens:\n{regimens_blob}\n\n"
        f"timeline_events (newest first):\n{timeline_blob}\n\n"
        "Produce the brief."
    )

    try:
        client = llm._get_client()
        if client is None:
            raise RuntimeError("LLM client unavailable")

        from services.orchestrator.llm_tracking import track_llm_call

        async with track_llm_call(
            call_kind="visit_brief", model=model_name
        ) as tracker:
            completion = await client.chat.completions.parse(
                model=model_name,
                response_format=_BriefBody,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_payload},
                ],
            )
            tracker.set_completion(completion)

        parsed: _BriefBody | None = (
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

        summary = (parsed.summary or "").strip()[:MAX_SUMMARY_CHARS]
        talking_points = _truncate_list(
            parsed.talking_points,
            cap=MAX_TALKING_POINTS,
            max_chars=140,
        )
        red_flags = _truncate_list(
            parsed.red_flags, cap=MAX_RED_FLAGS, max_chars=140
        )
        if not summary:
            raise RuntimeError("LLM returned empty summary")

        brief = await visit_briefs_repo.create(
            session,
            patient_id=patient_id,
            appointment_id=appointment_id,
            doctor_id=doctor_id,
            window_start=since,
            window_end=until,
            llm_model=model_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            summary=summary,
            talking_points=talking_points,
            red_flags=red_flags,
            key_metrics=key_metrics,
            generated_by=generated_by,
        )
        if appointment_id is not None:
            await visit_briefs_repo.supersede_for_appointment(
                session,
                appointment_id=appointment_id,
                keep_brief_id=brief.id,
            )
        return brief

    except Exception as exc:  # noqa: BLE001 — record + re-raise
        log.warning(
            "visit brief generation failed for patient %s: %s",
            patient_id,
            exc,
        )
        await visit_briefs_repo.create_failed(
            session,
            patient_id=patient_id,
            appointment_id=appointment_id,
            doctor_id=doctor_id,
            window_start=since,
            window_end=until,
            llm_model=model_name,
            error=str(exc),
            generated_by=generated_by,
        )
        # Commit the audit-trail row before re-raising —
        # otherwise the caller's exception handler aborts the
        # transaction and the failed-brief row vanishes,
        # leaving ops with no record of the attempt.
        await session.commit()
        raise RuntimeError(
            f"visit brief generation failed: {exc}"
        ) from exc

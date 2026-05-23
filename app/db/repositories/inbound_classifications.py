from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import InboundClassification


# Allowlist of values written to the ``category`` column. Validated at
# write-time (not via DB CHECK) so adding a category is a constants-only
# change. ``action_tap`` is a sentinel for tap-routed messages we record
# without an LLM call so they show up in the inbox alongside everything
# else; ``unknown`` is for classifier failures.
KNOWN_CATEGORIES: tuple[str, ...] = (
    "clinical_question",
    "administrative",
    "billing",
    "scheduling",
    "faq",
    "social",
    "unsafe",
    "action_tap",
    "unknown",
)
KNOWN_URGENCIES: tuple[str, ...] = ("critical", "high", "medium", "low")

# How the patient sent the inbound. ``voice`` = transcribed audio note,
# ``image`` = photo/document upload (with optional caption), ``button``
# = tap on a quick-reply / list-row, ``text`` = typed plain text. Drives
# the inbox UI badge so a clinician can spot transcription-quality
# issues without re-listening to the audio.
KNOWN_INPUT_KINDS: tuple[str, ...] = ("text", "voice", "image", "button")


def _coerce_input_kind(value: str | None) -> str:
    if value and value in KNOWN_INPUT_KINDS:
        return value
    return "text"


def _coerce_category(value: str | None) -> str:
    if value and value in KNOWN_CATEGORIES:
        return value
    return "unknown"


def _coerce_urgency(value: str | None) -> str:
    if value and value in KNOWN_URGENCIES:
        return value
    return "low"


def _trim(value: str | None, *, limit: int) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    return text[:limit]


async def create(
    session: AsyncSession,
    *,
    message_id: str | None,
    patient_phone: str,
    patient_db_id: int | None,
    inbound_text: str | None,
    category: str,
    summary: str | None,
    urgency: str,
    handler_used: str | None,
    response_text: str | None,
    escalated: bool,
    ticket_id: int | None,
    input_kind: str = "text",
    request_duration_ms: int | None = None,
) -> InboundClassification:
    row = InboundClassification(
        message_id=_trim(message_id, limit=128),
        patient_phone=patient_phone,
        patient_db_id=patient_db_id,
        # Cap the body fields at a generous-but-bounded length so a
        # pasted novel doesn't bloat the table. The full message lives
        # in ``message_log`` if anyone needs the original.
        inbound_text=_trim(inbound_text, limit=4000),
        input_kind=_coerce_input_kind(input_kind),
        category=_coerce_category(category),
        summary=_trim(summary, limit=500),
        urgency=_coerce_urgency(urgency),
        handler_used=_trim(handler_used, limit=64),
        response_text=_trim(response_text, limit=4000),
        escalated=escalated,
        ticket_id=ticket_id,
        request_duration_ms=request_duration_ms,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def get(
    session: AsyncSession, classification_id: int
) -> InboundClassification | None:
    """Single-row fetch by primary key. Used by the slice-13
    drafter endpoint and the feedback path."""
    return await session.get(
        InboundClassification, classification_id
    )


async def list_recent(
    session: AsyncSession,
    *,
    limit: int = 100,
    category: str | None = None,
    urgency: str | None = None,
    escalated: bool | None = None,
    patient_phone: str | None = None,
    input_kind: str | None = None,
    since: datetime | None = None,
) -> list[InboundClassification]:
    """Query for the inbox view. All filters are optional; results are
    ordered newest-first."""
    stmt = (
        select(InboundClassification)
        .order_by(desc(InboundClassification.created_at))
        .limit(limit)
    )
    if category is not None:
        stmt = stmt.where(InboundClassification.category == category)
    if urgency is not None:
        stmt = stmt.where(InboundClassification.urgency == urgency)
    if escalated is not None:
        stmt = stmt.where(InboundClassification.escalated.is_(escalated))
    if patient_phone is not None:
        stmt = stmt.where(InboundClassification.patient_phone == patient_phone)
    if input_kind is not None:
        stmt = stmt.where(InboundClassification.input_kind == input_kind)
    if since is not None:
        stmt = stmt.where(InboundClassification.created_at >= since)
    return list((await session.execute(stmt)).scalars().all())


async def set_feedback(
    session: AsyncSession,
    classification_id: int,
    *,
    rating: int,
    actor: str,
    note: str | None = None,
    when: datetime | None = None,
) -> InboundClassification | None:
    """Stamp the bot-reply quality feedback on an inbox row.

    ``rating`` must be +1 (thumbs-up) or -1 (thumbs-down) — any
    other value is rejected at the endpoint layer. The function
    overwrites prior feedback so a doctor's rating can supersede
    an ops thumbs-down (or vice versa). The ``feedback_at``
    timestamp records when the LATEST rating landed.
    """
    if rating not in (-1, 1):
        raise ValueError(f"rating must be -1 or 1; got {rating}")
    row = await session.get(InboundClassification, classification_id)
    if row is None:
        return None
    row.feedback_rating = rating
    row.feedback_note = (note or "").strip()[:1000] or None
    row.feedback_by = actor
    row.feedback_at = when or datetime.now(timezone.utc)
    await session.flush()
    return row


async def clear_feedback(
    session: AsyncSession, classification_id: int
) -> InboundClassification | None:
    """Remove a feedback rating — used when an operator
    accidentally thumbs-up'd the wrong row."""
    row = await session.get(InboundClassification, classification_id)
    if row is None:
        return None
    row.feedback_rating = None
    row.feedback_note = None
    row.feedback_by = None
    row.feedback_at = None
    await session.flush()
    return row


async def category_counts(
    session: AsyncSession, *, since: datetime | None = None
) -> dict[str, int]:
    """{category → count} since ``since`` (or all-time if None). Powers
    the inbox header widget."""
    from sqlalchemy import func

    stmt = select(
        InboundClassification.category, func.count(InboundClassification.id)
    ).group_by(InboundClassification.category)
    if since is not None:
        stmt = stmt.where(InboundClassification.created_at >= since)
    rows = (await session.execute(stmt)).all()
    out: dict[str, int] = {c: 0 for c in KNOWN_CATEGORIES}
    for cat, count in rows:
        out[cat] = count
    return out

"""Doctor inbox (inbound classifications) + reply-drafter endpoints.

Extracted from main.py. The ops-console inbox: list/filter classified inbound
messages, category counts, thumbs feedback, and an LLM draft-reply helper
(review-only — never auto-sends).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import (
    inbound_classifications as inbound_classifications_repo,
)
from app.db.repositories import patients as patients_repo
from app.db.session import get_session

router = APIRouter()


class InboundClassificationDTO(BaseModel):
    id: int
    message_id: str | None
    patient_phone: str
    patient_db_id: int | None
    patient_full_name: str | None = None
    inbound_text: str | None
    input_kind: str
    category: str
    summary: str | None
    urgency: str
    handler_used: str | None
    response_text: str | None
    escalated: bool
    ticket_id: int | None
    created_at: datetime
    # Bot-reply quality feedback (+1, -1, or null = no rating).
    feedback_rating: int | None = None
    feedback_note: str | None = None
    feedback_by: str | None = None
    feedback_at: datetime | None = None
    # Triage classifier verdict — non-null only when the
    # message tripped a clinical_alerts row. UI renders the
    # severity badge inline so doctors scanning inbox spot
    # alert-tied messages without bouncing to /clinical-alerts.
    clinical_severity: str | None = None


def _classification_to_dto(
    row: Any, *, patient_full_name: str | None = None
) -> InboundClassificationDTO:
    return InboundClassificationDTO(
        id=row.id,
        message_id=row.message_id,
        patient_phone=row.patient_phone,
        patient_db_id=row.patient_db_id,
        patient_full_name=patient_full_name,
        inbound_text=row.inbound_text,
        input_kind=row.input_kind,
        category=row.category,
        summary=row.summary,
        urgency=row.urgency,
        handler_used=row.handler_used,
        response_text=row.response_text,
        escalated=row.escalated,
        ticket_id=row.ticket_id,
        created_at=row.created_at,
        feedback_rating=getattr(row, "feedback_rating", None),
        feedback_note=getattr(row, "feedback_note", None),
        feedback_by=getattr(row, "feedback_by", None),
        feedback_at=getattr(row, "feedback_at", None),
        clinical_severity=getattr(row, "clinical_severity", None),
    )


@router.get(
    "/ops/inbox", response_model=list[InboundClassificationDTO]
)
async def list_inbox(
    db: AsyncSession = Depends(get_session),
    category: str | None = None,
    urgency: str | None = None,
    escalated: bool | None = None,
    patient_phone: str | None = None,
    input_kind: str | None = None,
    limit: int = 100,
) -> list[InboundClassificationDTO]:
    """Recent inbound classifications, newest first. Powers the ops
    console /inbox view. Filters compose with AND."""
    rows = await inbound_classifications_repo.list_recent(
        db,
        limit=max(1, min(limit, 500)),
        category=category,
        urgency=urgency,
        escalated=escalated,
        patient_phone=patient_phone,
        input_kind=input_kind,
    )
    # Resolve patient names by phone — small batch, one lookup per
    # unique phone in the result set.
    phone_to_name: dict[str, str | None] = {}
    for row in rows:
        if row.patient_phone not in phone_to_name:
            patient = await patients_repo.get_by_phone(db, row.patient_phone)
            phone_to_name[row.patient_phone] = (
                patient.full_name if patient else None
            )
    return [
        _classification_to_dto(
            r, patient_full_name=phone_to_name.get(r.patient_phone)
        )
        for r in rows
    ]


@router.get("/ops/inbox/category-counts")
async def inbox_category_counts(
    db: AsyncSession = Depends(get_session),
    days: int = 7,
) -> dict[str, int]:
    """{category → count} over the last ``days`` days. Powers the
    inbox header chips."""
    since = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    return await inbound_classifications_repo.category_counts(
        db, since=since
    )


class InboxFeedbackRequest(BaseModel):
    rating: Literal[-1, 1]
    actor: str = Field(min_length=1, max_length=128)
    note: str | None = Field(default=None, max_length=1000)


@router.post(
    "/ops/inbox/{classification_id}/feedback",
    response_model=InboundClassificationDTO,
)
async def set_inbox_feedback(
    classification_id: int,
    payload: InboxFeedbackRequest,
    db: AsyncSession = Depends(get_session),
) -> InboundClassificationDTO:
    """Record a thumbs-up / thumbs-down on a bot reply. Rating is
    ``+1`` (good reply) or ``-1`` (bad reply); the note field
    captures optional free-form context (especially useful on
    thumbs-down rows). Subsequent calls overwrite — a doctor's
    review can supersede an ops thumbs-down without us tracking
    a full history per row in v1."""
    row = await inbound_classifications_repo.set_feedback(
        db,
        classification_id,
        rating=payload.rating,
        actor=payload.actor,
        note=payload.note,
    )
    if row is None:
        raise HTTPException(
            status_code=404, detail="classification not found"
        )
    await db.commit()
    return _classification_to_dto(row)


@router.delete(
    "/ops/inbox/{classification_id}/feedback",
    response_model=InboundClassificationDTO,
)
async def clear_inbox_feedback(
    classification_id: int,
    db: AsyncSession = Depends(get_session),
) -> InboundClassificationDTO:
    """Clear a feedback rating — used when an operator
    accidentally rated the wrong row."""
    row = await inbound_classifications_repo.clear_feedback(
        db, classification_id
    )
    if row is None:
        raise HTTPException(
            status_code=404, detail="classification not found"
        )
    await db.commit()
    return _classification_to_dto(row)


class DraftReplyDTO(BaseModel):
    """LLM draft for a doctor reply. The reviewer always edits
    + sends manually; this endpoint just reduces typing time
    for routine acknowledgements."""

    draft_text: str
    confidence: str
    caveats: list[str] = Field(default_factory=list)
    llm_model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@router.post(
    "/ops/inbox/{classification_id}/draft-reply",
    response_model=DraftReplyDTO,
)
async def draft_reply_for_inbox_row(
    classification_id: int,
    db: AsyncSession = Depends(get_session),
) -> DraftReplyDTO:
    """Generate a context-aware draft reply for a specific
    inbox row. Returns ``draft_text`` (possibly empty when LLM
    failed) plus ``confidence`` + ``caveats`` so the UI can
    surface uncertainty inline. Never sends to the patient —
    that's a separate explicit action."""
    from services.orchestrator import doctor_reply_drafter

    row = await inbound_classifications_repo.get(
        db, classification_id
    )
    if row is None:
        raise HTTPException(
            status_code=404, detail="classification not found"
        )
    if not row.inbound_text or not row.inbound_text.strip():
        # Action-tap and image-only rows have no body text;
        # don't burn tokens on them. UI hides the draft button
        # when there's nothing to reply to anyway.
        raise HTTPException(
            status_code=400,
            detail="inbox row has no inbound text to draft against",
        )
    if row.patient_db_id is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "inbox row has no resolved patient — can't "
                "personalise a draft"
            ),
        )

    patient = await patients_repo.get(db, row.patient_db_id)
    first_name: str | None = None
    if patient is not None and patient.full_name:
        first_name = patient.full_name.split(" ", 1)[0]

    draft = await doctor_reply_drafter.draft_reply(
        db,
        patient_id=row.patient_db_id,
        inbound_text=row.inbound_text,
        patient_first_name=first_name,
    )
    return DraftReplyDTO(
        draft_text=draft.draft_text,
        confidence=draft.confidence,
        caveats=list(draft.caveats),
        llm_model=draft.llm_model,
        prompt_tokens=draft.prompt_tokens,
        completion_tokens=draft.completion_tokens,
    )

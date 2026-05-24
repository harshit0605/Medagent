"""Broadcast campaign (cohort bulk send) endpoints. Extracted from main.py.

Create resolves a cohort + enqueues per-recipient scheduled events (respecting
consent / bot-pause / erasure gates); list/detail/recipients power the ops
campaign views.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session

router = APIRouter()


class BroadcastCampaignDTO(BaseModel):
    id: int
    name: str
    template_name: str
    template_params: dict[str, Any]
    cohort_filter: dict[str, Any]
    status: str
    created_by: str | None = None
    created_at: datetime
    materialised_at: datetime | None = None
    completed_at: datetime | None = None
    total_recipients: int
    sent_count: int
    skipped_count: int
    notes: str | None = None


class BroadcastSendDTO(BaseModel):
    id: int
    campaign_id: int
    patient_id: str
    patient_db_id: int | None = None
    status: str
    skip_reason: str | None = None
    scheduled_event_id: int | None = None
    created_at: datetime


class BroadcastCampaignDetailDTO(BaseModel):
    campaign: BroadcastCampaignDTO
    counts_by_status: dict[str, int]
    counts_by_skip_reason: dict[str, int]


class BroadcastCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    template_name: str = Field(min_length=1, max_length=128)
    template_params: dict[str, Any] = Field(default_factory=dict)
    cohort_filter: dict[str, Any]
    created_by: str = Field(min_length=1, max_length=128)
    notes: str | None = Field(default=None, max_length=2000)
    materialise_immediately: bool = True


def _campaign_to_dto(row: Any) -> BroadcastCampaignDTO:
    return BroadcastCampaignDTO(
        id=row.id,
        name=row.name,
        template_name=row.template_name,
        template_params=row.template_params or {},
        cohort_filter=row.cohort_filter or {},
        status=row.status,
        created_by=row.created_by,
        created_at=row.created_at,
        materialised_at=row.materialised_at,
        completed_at=row.completed_at,
        total_recipients=row.total_recipients or 0,
        sent_count=row.sent_count or 0,
        skipped_count=row.skipped_count or 0,
        notes=row.notes,
    )


async def _count_sends_by_skip_reason(
    db: AsyncSession, campaign_id: int
) -> dict[str, int]:
    """Per-skip-reason histogram. Used by the detail view to
    show the 'skipped breakdown' tile (opted-out vs paused vs
    erased)."""
    from sqlalchemy import func, select

    from app.db.models import BroadcastSend

    stmt = (
        select(BroadcastSend.skip_reason, func.count())
        .where(BroadcastSend.campaign_id == campaign_id)
        .where(BroadcastSend.skip_reason.is_not(None))
        .group_by(BroadcastSend.skip_reason)
    )
    rows = (await db.execute(stmt)).all()
    return {reason: int(count) for reason, count in rows}


@router.post("/campaigns", response_model=BroadcastCampaignDetailDTO)
async def create_broadcast_campaign(
    payload: BroadcastCreateRequest,
    db: AsyncSession = Depends(get_session),
) -> BroadcastCampaignDetailDTO:
    """Create a broadcast campaign. With ``materialise_immediately``
    (default True), resolves the cohort + enqueues per-recipient
    scheduled events in the SAME request — the dispatcher's next
    tick picks them up. With False, leaves the campaign in
    ``draft`` status; ops triggers materialisation later via
    POST /campaigns/{id}/materialise.

    Per-recipient sends respect the existing consent + bot-pause
    + erasure gates: ineligible patients land in
    ``broadcast_sends`` with ``status="skipped"`` and a
    ``skip_reason`` so the campaign detail view shows
    "120 sent, 5 opted-out, 2 paused" up front."""
    from app.db.repositories import (
        broadcast_campaigns as broadcast_campaigns_repo,
    )
    from services.orchestrator import broadcast_service

    # Validate cohort_filter shape — fail fast at create time
    # rather than discover the issue mid-materialise.
    cohort = payload.cohort_filter.get("cohort") if isinstance(
        payload.cohort_filter, dict
    ) else None
    if cohort not in {"diabetes", "cardiac", "fall_risk"}:
        raise HTTPException(
            status_code=400,
            detail=(
                "cohort_filter must be {'cohort': "
                "'diabetes' | 'cardiac' | 'fall_risk'} for v1"
            ),
        )

    campaign = await broadcast_campaigns_repo.create(
        db,
        name=payload.name,
        template_name=payload.template_name,
        template_params=payload.template_params,
        cohort_filter=payload.cohort_filter,
        created_by=payload.created_by,
        notes=payload.notes,
    )
    if payload.materialise_immediately:
        try:
            await broadcast_service.materialise_campaign(
                db, campaign_id=campaign.id
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=400, detail=str(exc)
            ) from exc
    await db.commit()

    refreshed = await broadcast_campaigns_repo.get(db, campaign.id)
    counts_by_status = (
        await broadcast_campaigns_repo.count_sends_by_status(
            db, campaign.id
        )
    )
    counts_by_skip_reason = await _count_sends_by_skip_reason(
        db, campaign.id
    )
    return BroadcastCampaignDetailDTO(
        campaign=_campaign_to_dto(refreshed),
        counts_by_status=counts_by_status,
        counts_by_skip_reason=counts_by_skip_reason,
    )


@router.get("/campaigns", response_model=list[BroadcastCampaignDTO])
async def list_broadcast_campaigns(
    db: AsyncSession = Depends(get_session),
    limit: int = 100,
) -> list[BroadcastCampaignDTO]:
    """Newest-first list of campaigns with their
    materialisation counts. Powers the /campaigns ops page."""
    if limit <= 0 or limit > 500:
        raise HTTPException(
            status_code=400, detail="limit must be in [1, 500]"
        )
    from app.db.repositories import (
        broadcast_campaigns as broadcast_campaigns_repo,
    )

    rows = await broadcast_campaigns_repo.list_recent(
        db, limit=limit
    )
    return [_campaign_to_dto(r) for r in rows]


@router.get(
    "/campaigns/{campaign_id}",
    response_model=BroadcastCampaignDetailDTO,
)
async def get_broadcast_campaign(
    campaign_id: int, db: AsyncSession = Depends(get_session)
) -> BroadcastCampaignDetailDTO:
    """Campaign detail with progress breakdown — counts by
    delivery status + skip-reason histogram."""
    from app.db.repositories import (
        broadcast_campaigns as broadcast_campaigns_repo,
    )

    campaign = await broadcast_campaigns_repo.get(db, campaign_id)
    if campaign is None:
        raise HTTPException(
            status_code=404, detail="campaign not found"
        )
    counts_by_status = (
        await broadcast_campaigns_repo.count_sends_by_status(
            db, campaign_id
        )
    )
    counts_by_skip_reason = await _count_sends_by_skip_reason(
        db, campaign_id
    )
    return BroadcastCampaignDetailDTO(
        campaign=_campaign_to_dto(campaign),
        counts_by_status=counts_by_status,
        counts_by_skip_reason=counts_by_skip_reason,
    )


@router.get(
    "/campaigns/{campaign_id}/recipients",
    response_model=list[BroadcastSendDTO],
)
async def list_broadcast_recipients(
    campaign_id: int,
    status: str | None = None,
    limit: int = 200,
    offset: int = 0,
    db: AsyncSession = Depends(get_session),
) -> list[BroadcastSendDTO]:
    """Per-recipient breakdown for the campaign detail page.
    Filterable by ``status`` (pending / skipped) so ops can
    drill into "show me everyone who got skipped for opt-out"."""
    if limit <= 0 or limit > 500:
        raise HTTPException(
            status_code=400, detail="limit must be in [1, 500]"
        )
    from app.db.repositories import (
        broadcast_campaigns as broadcast_campaigns_repo,
    )

    rows = await broadcast_campaigns_repo.list_sends(
        db,
        campaign_id,
        status=status,
        limit=limit,
        offset=max(0, offset),
    )
    return [
        BroadcastSendDTO(
            id=r.id,
            campaign_id=r.campaign_id,
            patient_id=r.patient_id,
            patient_db_id=r.patient_db_id,
            status=r.status,
            skip_reason=r.skip_reason,
            scheduled_event_id=r.scheduled_event_id,
            created_at=r.created_at,
        )
        for r in rows
    ]

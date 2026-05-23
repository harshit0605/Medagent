"""Repo helpers for broadcast_campaigns + broadcast_sends.

The orchestration logic (recipient resolution + scheduled-event
enqueue + gate-respecting send creation) lives in
``services.orchestrator.broadcast_service``. This module is the
persistence boundary — bookkeeping queries only.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import BroadcastCampaign, BroadcastSend


async def create(
    session: AsyncSession,
    *,
    name: str,
    template_name: str,
    template_params: dict[str, Any] | None = None,
    cohort_filter: dict[str, Any] | None = None,
    created_by: str | None = None,
    notes: str | None = None,
) -> BroadcastCampaign:
    row = BroadcastCampaign(
        name=name,
        template_name=template_name,
        template_params=template_params or {},
        cohort_filter=cohort_filter or {},
        status="draft",
        created_by=created_by,
        notes=notes,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def get(
    session: AsyncSession, campaign_id: int
) -> BroadcastCampaign | None:
    return await session.get(BroadcastCampaign, campaign_id)


async def list_recent(
    session: AsyncSession, *, limit: int = 100
) -> list[BroadcastCampaign]:
    stmt = (
        select(BroadcastCampaign)
        .order_by(desc(BroadcastCampaign.created_at))
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def update_after_materialise(
    session: AsyncSession,
    campaign_id: int,
    *,
    total_recipients: int,
    sent_count: int,
    skipped_count: int,
    when: datetime | None = None,
) -> BroadcastCampaign | None:
    """Stamp the campaign with materialisation outcomes. Status
    moves to ``materialised`` — the campaign is "done" from the
    broadcast layer's POV; per-recipient delivery state lives on
    the linked scheduled_events from here on."""
    row = await session.get(BroadcastCampaign, campaign_id)
    if row is None:
        return None
    row.status = "materialised"
    row.materialised_at = when or datetime.now(timezone.utc)
    row.total_recipients = total_recipients
    row.sent_count = sent_count
    row.skipped_count = skipped_count
    await session.flush()
    await session.refresh(row)
    return row


async def add_send(
    session: AsyncSession,
    *,
    campaign_id: int,
    patient_id: str,
    patient_db_id: int | None,
    status: str,
    skip_reason: str | None = None,
    scheduled_event_id: int | None = None,
) -> BroadcastSend:
    row = BroadcastSend(
        campaign_id=campaign_id,
        patient_id=patient_id,
        patient_db_id=patient_db_id,
        status=status,
        skip_reason=skip_reason,
        scheduled_event_id=scheduled_event_id,
    )
    session.add(row)
    await session.flush()
    return row


async def list_sends(
    session: AsyncSession,
    campaign_id: int,
    *,
    status: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[BroadcastSend]:
    stmt = (
        select(BroadcastSend)
        .where(BroadcastSend.campaign_id == campaign_id)
        .order_by(BroadcastSend.id)
        .limit(limit)
        .offset(offset)
    )
    if status is not None:
        stmt = stmt.where(BroadcastSend.status == status)
    return list((await session.execute(stmt)).scalars().all())


async def count_sends_by_status(
    session: AsyncSession, campaign_id: int
) -> dict[str, int]:
    """Per-status counts for the campaign's recipients. Powers
    the detail-page progress view + the completion tally."""
    stmt = (
        select(BroadcastSend.status, func.count())
        .where(BroadcastSend.campaign_id == campaign_id)
        .group_by(BroadcastSend.status)
    )
    rows = (await session.execute(stmt)).all()
    return {status: int(count) for status, count in rows}

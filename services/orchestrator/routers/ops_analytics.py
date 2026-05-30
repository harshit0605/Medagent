"""Program-level outcome analytics endpoint (/ops/analytics).

Extracted from main.py. Adherence, recap funnel, inbox composition, ops-queue
throughput + daily time-series for the analytics page sparklines.
(``/ops/dashboard`` stays in main — it's coupled to the ops-tickets cluster's
ProgramDashboardDTO.)
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import dashboard as dashboard_repo
from app.db.repositories import inbound_classifications as inbound_repo
from app.db.session import get_session

router = APIRouter()


class HandlerQualityRowDTO(BaseModel):
    handler: str
    total_rated: int
    thumbs_up: int
    thumbs_down: int
    up_rate: float


@router.get(
    "/ops/analytics/handler-quality",
    response_model=list[HandlerQualityRowDTO],
)
async def get_handler_quality(
    db: AsyncSession = Depends(get_session),
    window_days: int = 30,
) -> list[HandlerQualityRowDTO]:
    """Per-handler reply-quality monitor (F6): operator/doctor thumbs feedback
    grouped by the handler that answered, worst up-rate first. Drives
    prompt/template improvement on the handlers operators rate poorly."""
    if window_days < 1 or window_days > 365:
        raise HTTPException(
            status_code=400, detail="window_days must be between 1 and 365"
        )
    rows = await inbound_repo.handler_quality(db, window_days=window_days)
    return [HandlerQualityRowDTO(**r) for r in rows]


class AdherenceSnapshotDTO(BaseModel):
    total: int
    taken: int
    missed: int
    skipped: int
    delayed: int
    scheduled: int
    rate: float


class RecapFunnelDTO(BaseModel):
    draft: int
    sent: int
    acknowledged: int
    questioned: int
    sent_total: int
    ack_rate: float


class InboxCompositionDTO(BaseModel):
    by_category: dict[str, int]
    by_urgency: dict[str, int]
    by_input_kind: dict[str, int]


class OpsQueueAnalyticsDTO(BaseModel):
    open_total: int
    by_priority: dict[str, int]
    opened_in_window: int
    resolved_in_window: int
    median_resolve_minutes: float | None


class AdherenceBucketDTO(BaseModel):
    date: str
    taken: int
    missed: int
    skipped: int
    delayed: int
    scheduled: int
    rate: float


class InboxBucketDTO(BaseModel):
    date: str
    total: int
    critical: int
    high: int
    medium: int
    low: int


class RecapBucketDTO(BaseModel):
    date: str
    sent: int
    acked: int


class TicketBucketDTO(BaseModel):
    date: str
    opened: int
    resolved: int


class AnalyticsTimeseriesDTO(BaseModel):
    window_days: int
    adherence: list[AdherenceBucketDTO]
    inbox: list[InboxBucketDTO]
    recap: list[RecapBucketDTO]
    tickets: list[TicketBucketDTO]


class AnalyticsSnapshotDTO(BaseModel):
    window_days: int
    since: datetime
    adherence: AdherenceSnapshotDTO
    recap_funnel: RecapFunnelDTO
    inbox: InboxCompositionDTO
    ops_queue: OpsQueueAnalyticsDTO
    timeseries: AnalyticsTimeseriesDTO


@router.get("/ops/analytics", response_model=AnalyticsSnapshotDTO)
async def get_ops_analytics(
    db: AsyncSession = Depends(get_session),
    days: int = 30,
) -> AnalyticsSnapshotDTO:
    """Program-level outcome snapshot — adherence, recap funnel, inbox
    composition, ops queue throughput, plus daily time-series for the
    sparkline charts. Read-only; cheap; safe to poll from the
    analytics page on every render."""
    if days < 1 or days > 365:
        raise HTTPException(
            status_code=400, detail="days must be between 1 and 365"
        )
    snapshot = await dashboard_repo.analytics_snapshot(db, days=days)
    timeseries = await dashboard_repo.analytics_timeseries(db, days=days)
    return AnalyticsSnapshotDTO(
        window_days=snapshot["window_days"],
        since=snapshot["since"],
        adherence=AdherenceSnapshotDTO(**snapshot["adherence"]),
        recap_funnel=RecapFunnelDTO(**snapshot["recap_funnel"]),
        inbox=InboxCompositionDTO(**snapshot["inbox"]),
        ops_queue=OpsQueueAnalyticsDTO(**snapshot["ops_queue"]),
        timeseries=AnalyticsTimeseriesDTO(
            window_days=timeseries["window_days"],
            adherence=[
                AdherenceBucketDTO(**b) for b in timeseries["adherence"]
            ],
            inbox=[InboxBucketDTO(**b) for b in timeseries["inbox"]],
            recap=[RecapBucketDTO(**b) for b in timeseries["recap"]],
            tickets=[TicketBucketDTO(**b) for b in timeseries["tickets"]],
        ),
    )

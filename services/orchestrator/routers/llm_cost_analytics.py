"""LLM cost + latency analytics endpoint. Extracted from main.py."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session

router = APIRouter()


class LlmCallKindStatDTO(BaseModel):
    call_kind: str
    calls: int
    tokens: int
    cost_usd_micros: int


class LlmModelStatDTO(BaseModel):
    model: str
    calls: int
    tokens: int
    cost_usd_micros: int


class LlmTopPatientStatDTO(BaseModel):
    patient_id: str
    calls: int
    tokens: int
    cost_usd_micros: int


class LlmLatencyDTO(BaseModel):
    p50_ms: int | None = None
    p95_ms: int | None = None
    p99_ms: int | None = None
    mean_ms: int | None = None


class LlmCostAnalyticsDTO(BaseModel):
    since: datetime
    until: datetime
    total_calls: int
    total_tokens: int
    total_cost_usd_micros: int
    errors_count: int
    by_call_kind: list[LlmCallKindStatDTO]
    by_model: list[LlmModelStatDTO]
    top_patients: list[LlmTopPatientStatDTO]
    latency: LlmLatencyDTO


@router.get(
    "/ops/analytics/llm-cost",
    response_model=LlmCostAnalyticsDTO,
)
async def get_llm_cost_analytics(
    db: AsyncSession = Depends(get_session),
    days: int = 30,
) -> LlmCostAnalyticsDTO:
    """Aggregate LLM cost + latency across the bot. Drives the
    /analytics/llm-cost ops page so we can answer "are we
    operating at acceptable cost?" and "where are the latency
    outliers?" before scaling to more clinics.

    Cost is reported as USD micros (integer 10⁻⁶ USD) so summing
    across millions of rows stays exact. The UI converts to
    dollars at render time."""
    if days <= 0 or days > 365:
        raise HTTPException(
            status_code=400, detail="days must be in [1, 365]"
        )
    from app.db.repositories import llm_calls as llm_calls_repo

    until = datetime.now(timezone.utc)
    since = until - timedelta(days=days)
    summary = await llm_calls_repo.summarize(
        db, since=since, until=until
    )
    latency = await llm_calls_repo.latency_percentiles(
        db, since=since, until=until
    )
    top_patients = await llm_calls_repo.top_patients_by_cost(
        db, since=since, until=until, limit=10
    )
    return LlmCostAnalyticsDTO(
        since=since,
        until=until,
        total_calls=summary["total_calls"],
        total_tokens=summary["total_tokens"],
        total_cost_usd_micros=summary["total_cost_usd_micros"],
        errors_count=summary["errors_count"],
        by_call_kind=[
            LlmCallKindStatDTO(**row) for row in summary["by_call_kind"]
        ],
        by_model=[
            LlmModelStatDTO(**row) for row in summary["by_model"]
        ],
        top_patients=[
            LlmTopPatientStatDTO(**row) for row in top_patients
        ],
        latency=LlmLatencyDTO(**latency),
    )

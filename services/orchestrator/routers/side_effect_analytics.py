"""Side-effect frequency analytics endpoint. Extracted from main.py."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session

router = APIRouter()


class MedicationStatDTO(BaseModel):
    medication_name: str
    report_count: int
    patient_count: int
    top_symptoms: list[tuple[str, int]]


class CohortStatDTO(BaseModel):
    cohort: str
    report_count: int
    patient_count: int


class SymptomStatDTO(BaseModel):
    symptom: str
    count: int


class SideEffectAnalyticsDTO(BaseModel):
    since: datetime
    until: datetime
    total_reports: int
    unique_patients: int
    unique_medications: int
    by_medication: list[MedicationStatDTO]
    by_cohort: list[CohortStatDTO]
    top_symptoms: list[SymptomStatDTO]


@router.get(
    "/ops/analytics/side-effects",
    response_model=SideEffectAnalyticsDTO,
)
async def get_side_effect_analytics(
    db: AsyncSession = Depends(get_session),
    days: int = 30,
) -> SideEffectAnalyticsDTO:
    """Clinical-pattern view across the side-effect reports panel.

    Aggregates last-N-days reports into:
        - per-medication (cross-referenced against the patient's
          active regimens at report time — strict attribution to
          avoid false positives)
        - per-cohort (legacy diabetes/cardiac/fall_risk + an
          ``uncategorized`` bucket for patients in no cohort)
        - panel-wide top symptoms (vocabulary-bag keyword extract)
        - summary tiles

    Reports without a mentioned medication contribute to the
    symptom + cohort rollups but not the per-medication
    breakdown — strict attribution catches the high-confidence
    cases without misattributing reports to drugs the patient
    isn't on."""
    if days <= 0 or days > 365:
        raise HTTPException(
            status_code=400, detail="days must be in [1, 365]"
        )
    from services.orchestrator import side_effect_analytics

    until = datetime.now(timezone.utc)
    since = until - timedelta(days=days)
    result = await side_effect_analytics.compute_side_effect_analytics(
        db, since=since, until=until
    )
    return SideEffectAnalyticsDTO(
        since=result.since,
        until=result.until,
        total_reports=result.total_reports,
        unique_patients=result.unique_patients,
        unique_medications=result.unique_medications,
        by_medication=[
            MedicationStatDTO(
                medication_name=m.medication_name,
                report_count=m.report_count,
                patient_count=m.patient_count,
                top_symptoms=[(s, c) for s, c in m.top_symptoms],
            )
            for m in result.by_medication
        ],
        by_cohort=[
            CohortStatDTO(
                cohort=c.cohort,
                report_count=c.report_count,
                patient_count=c.patient_count,
            )
            for c in result.by_cohort
        ],
        top_symptoms=[
            SymptomStatDTO(symptom=s.symptom, count=s.count)
            for s in result.top_symptoms
        ],
    )

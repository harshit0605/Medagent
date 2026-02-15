from datetime import datetime, timezone

from fastapi import FastAPI
from pydantic import BaseModel, Field

from shared.contracts.models import Event, EventType

app = FastAPI(title="scheduler")


class DoseDueRequest(BaseModel):
    patient_id: str = Field(min_length=1)
    regimen_id: str = Field(min_length=1)


class RefillDueRequest(BaseModel):
    patient_id: str = Field(min_length=1)
    medication_name: str = Field(min_length=1)
    days_left: int = Field(ge=0)


class TriageAlertRequest(BaseModel):
    patient_id: str = Field(min_length=1)
    cohort: str = Field(min_length=1)
    severity: str = Field(pattern="^(low|medium|high|critical)$")
    reason: str = Field(min_length=3)


class FollowupClosureRequest(BaseModel):
    patient_id: str = Field(min_length=1)
    followup_type: str = Field(pattern="^(lab|appointment)$")
    item_name: str = Field(min_length=1)
    status: str = Field(pattern="^(due|booked|completed|reviewed)$")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/emit-dose-due")
def emit_dose_due(payload: DoseDueRequest) -> Event:
    now = datetime.now(timezone.utc)
    return Event(
        event_type=EventType.DOSE_DUE,
        patient_id=payload.patient_id,
        occurred_at=now,
        at=now,
        payload={"regimen_id": payload.regimen_id},
    )


@app.post("/emit-refill-due")
def emit_refill_due(payload: RefillDueRequest) -> Event:
    now = datetime.now(timezone.utc)
    stage = "d1" if payload.days_left <= 1 else "d3" if payload.days_left <= 3 else "d7"
    return Event(
        event_type=EventType.REFILL_DUE,
        patient_id=payload.patient_id,
        occurred_at=now,
        at=now,
        payload={
            "medication_name": payload.medication_name,
            "days_left": payload.days_left,
            "refill_stage": stage,
            "actions": "REORDER/UPDATE COUNT",
        },
    )


@app.post("/emit-triage-alert")
def emit_triage_alert(payload: TriageAlertRequest) -> Event:
    now = datetime.now(timezone.utc)
    return Event(
        event_type=EventType.TRIAGE_ALERT,
        patient_id=payload.patient_id,
        occurred_at=now,
        at=now,
        payload={
            "cohort": payload.cohort,
            "severity": payload.severity,
            "reason": payload.reason,
        },
    )


@app.post("/emit-followup-closure")
def emit_followup_closure(payload: FollowupClosureRequest) -> Event:
    now = datetime.now(timezone.utc)
    return Event(
        event_type=EventType.TRIAGE_ALERT,
        patient_id=payload.patient_id,
        occurred_at=now,
        at=now,
        payload={
            "followup_type": payload.followup_type,
            "item_name": payload.item_name,
            "status": payload.status,
        },
    )

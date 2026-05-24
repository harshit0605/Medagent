"""Ops health/observability + appointment demo-trigger endpoints.

Extracted from main.py. ``GET /ops/health`` is the production observability
snapshot (heartbeat freshness + failed/overdue event counts); the
appointment test-reminder is a demo/debug trigger that enqueues a reminder NOW.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import appointments as appointments_repo
from app.db.repositories import patients as patients_repo
from app.db.repositories import scheduled_events as scheduled_events_repo
from app.db.repositories import service_heartbeats as service_heartbeats_repo
from app.db.session import get_session

router = APIRouter()


# ---- Demo / debug: fire an appointment reminder NOW ------------------------


class TestReminderResponse(BaseModel):
    scheduled_event_id: int
    event_type: str
    scheduled_for: datetime
    note: str


@router.post(
    "/appointments/{appointment_id}/test-reminder",
    response_model=TestReminderResponse,
)
async def fire_test_reminder(
    appointment_id: int,
    kind: Literal["24h", "1h"] = "1h",
    db: AsyncSession = Depends(get_session),
) -> TestReminderResponse:
    """Enqueue a single appointment reminder due NOW.

    Useful for demos / manual testing — without this you'd have to wait until
    the real T-24h or T-1h tick to see one go out. The next scheduler poll
    cycle picks it up and the dispatcher runs the same code path as the
    timed reminders.
    """
    appointment = await appointments_repo.get(db, appointment_id)
    if appointment is None:
        raise HTTPException(status_code=404, detail="appointment not found")
    patient = await patients_repo.get(db, appointment.patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="patient row missing")

    event_type = f"appointment_reminder_{kind}"
    appt_start = appointment.scheduled_for
    if appt_start.tzinfo is None:
        appt_start = appt_start.replace(tzinfo=timezone.utc)

    row = await scheduled_events_repo.enqueue(
        db,
        event_type=event_type,
        patient_id=patient.phone,
        payload={
            "appointment_id": appointment.id,
            "doctor_id": appointment.doctor_id,
            "patient_db_id": appointment.patient_id,
            "appointment_start_iso": appt_start.isoformat(),
        },
        scheduled_for=datetime.now(timezone.utc),
    )
    await db.commit()
    return TestReminderResponse(
        scheduled_event_id=row.id,
        event_type=event_type,
        scheduled_for=row.scheduled_for,
        note="scheduler will pick this up on next tick (within SCHEDULER_POLL_SECONDS)",
    )


# ---- Health (production observability) ------------------------------------


class HeartbeatDTO(BaseModel):
    component: str
    last_run_at: datetime
    last_outcome: str
    details: dict
    consecutive_errors: int
    seconds_since_last_run: float
    is_stale: bool  # last_run_at older than the loop's expected cadence


# How long a heartbeat is allowed to be quiet before we consider the
# component "stale". Should be a small multiple of each loop's
# configured interval — if you bumped the interval env var without
# updating these, the worst case is a false-positive "stale" warning.
_STALE_THRESHOLD_SECONDS: dict[str, int] = {
    "scheduler.dispatch": 180,                # poll runs every 60s
    "scheduler.dose_materialize": 1800,       # 600s (10m) interval
    "scheduler.missed_dose_sweep": 900,       # 300s (5m) interval
    "scheduler.recap_sweep": 1800,            # 600s (10m) interval
    "scheduler.care_gap_sweep": 86400,        # 21600s (6h) interval
}


class HealthSummaryDTO(BaseModel):
    components: list[HeartbeatDTO]
    failed_events_24h: int
    pending_overdue: int  # pending events scheduled_for > 1h ago
    stuck_components: int  # components that are stale per their threshold
    error_components: int  # components whose last run was an error


def _heartbeat_to_dto(row: Any, now: datetime) -> HeartbeatDTO:
    last_run = row.last_run_at
    if last_run.tzinfo is None:
        last_run = last_run.replace(tzinfo=timezone.utc)
    seconds = max(0.0, (now - last_run).total_seconds())
    threshold = _STALE_THRESHOLD_SECONDS.get(row.component, 3600)
    return HeartbeatDTO(
        component=row.component,
        last_run_at=last_run,
        last_outcome=row.last_outcome,
        details=row.details or {},
        consecutive_errors=row.consecutive_errors or 0,
        seconds_since_last_run=seconds,
        is_stale=seconds > threshold,
    )


async def _failed_scheduled_events_24h(db: AsyncSession) -> int:
    from app.db.models import ScheduledEvent, ScheduledEventStatus
    from sqlalchemy import func, select

    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    stmt = select(func.count(ScheduledEvent.id)).where(
        ScheduledEvent.status == ScheduledEventStatus.failed
    ).where(ScheduledEvent.created_at >= cutoff)
    return (await db.execute(stmt)).scalar_one()


async def _pending_overdue_count(db: AsyncSession) -> int:
    """Pending events whose scheduled_for is older than 1h. Indicates
    the dispatcher loop is stuck or the queue is backed up."""
    from app.db.models import ScheduledEvent, ScheduledEventStatus
    from sqlalchemy import func, select

    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    stmt = select(func.count(ScheduledEvent.id)).where(
        ScheduledEvent.status == ScheduledEventStatus.pending
    ).where(ScheduledEvent.scheduled_for <= cutoff)
    return (await db.execute(stmt)).scalar_one()


@router.get("/ops/health", response_model=HealthSummaryDTO)
async def get_ops_health(
    db: AsyncSession = Depends(get_session),
) -> HealthSummaryDTO:
    """Production observability snapshot:
      - heartbeat freshness per scheduler loop (with per-component
        staleness threshold)
      - count of failed scheduled events in the last 24h
      - count of pending events whose scheduled_for has elapsed by >1h
        (stuck dispatcher signal)

    Read-only; cheap; safe to poll from a status page."""
    now = datetime.now(timezone.utc)
    rows = await service_heartbeats_repo.list_all(db)
    component_dtos = [_heartbeat_to_dto(r, now) for r in rows]
    return HealthSummaryDTO(
        components=component_dtos,
        failed_events_24h=await _failed_scheduled_events_24h(db),
        pending_overdue=await _pending_overdue_count(db),
        stuck_components=sum(1 for c in component_dtos if c.is_stale),
        error_components=sum(
            1 for c in component_dtos if c.last_outcome == "error"
        ),
    )

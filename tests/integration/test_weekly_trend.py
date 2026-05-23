"""Integration tests for the proactive weekly-trend push (DB-backed).

Skipped when DATABASE_URL is unset.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db.models import Patient, ScheduledEvent
from app.db.repositories import care_plan_goals as goals_repo
from app.db.session import get_sessionmaker
from services.scheduler import dispatcher, weekly_trend_sweep

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping weekly-trend tests",
)


async def _seed_patient() -> tuple[int, str]:
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(
            full_name=f"Trend {suffix}",
            phone=f"trend-{suffix}",
            consent_sms=True,
        )
        db.add(p)
        await db.flush()
        await db.commit()
        return p.id, p.phone


async def _record(pid: int, metric_key: str, value: str, *, days_ago: int):
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await goals_repo.record_observation(
            db,
            patient_id=pid,
            goal_id=None,
            metric_key=metric_key,
            value=Decimal(value),
            unit="mg/dL",
            source="patient_self_report",
            observed_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
        )
        await db.commit()


async def _trend_events(phone: str) -> list[ScheduledEvent]:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        stmt = (
            select(ScheduledEvent)
            .where(ScheduledEvent.patient_id == phone)
            .where(ScheduledEvent.event_type == "weekly_trend_push")
        )
        return list((await db.execute(stmt)).scalars().all())


async def test_sweep_pushes_and_dedupes():
    pid, phone = await _seed_patient()
    await _record(pid, "blood_glucose", "140", days_ago=1)
    await _record(pid, "blood_glucose", "150", days_ago=3)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await weekly_trend_sweep.sweep_weekly_trends(db)
        await db.commit()

    events = await _trend_events(phone)
    assert len(events) == 1
    summary = events[0].payload["summary"]
    glucose = next(s for s in summary if s["metric_key"] == "blood_glucose")
    assert glucose["count"] == 2

    # Re-running within the window does not push again.
    async with SessionLocal() as db:
        await weekly_trend_sweep.sweep_weekly_trends(db)
        await db.commit()
    assert len(await _trend_events(phone)) == 1


async def test_sweep_skips_stale_readings():
    pid, phone = await _seed_patient()
    await _record(pid, "blood_glucose", "140", days_ago=30)  # outside window

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await weekly_trend_sweep.sweep_weekly_trends(db)
        await db.commit()
    assert await _trend_events(phone) == []


async def test_dispatcher_builds_weekly_trend_template():
    _pid, phone = await _seed_patient()
    event = SimpleNamespace(
        id=8101,
        event_type="weekly_trend_push",
        patient_id=phone,
        payload={
            "summary": [
                {
                    "metric_key": "blood_glucose",
                    "label": "Blood glucose",
                    "unit": "mg/dL",
                    "count": 2,
                    "latest": "140",
                }
            ]
        },
    )
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        msg = await dispatcher._build_weekly_trend(db, event)
    # No prior inbound → out-of-CSW → template.
    assert msg["use_template"] is True
    assert msg["template_name"] == "weekly_trend_v1"
    assert "Blood glucose" in msg["template_params"]["1_summary"]

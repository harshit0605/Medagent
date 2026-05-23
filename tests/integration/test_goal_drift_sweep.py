"""Integration tests for the goal drift sweep (slice 17).

The drift evaluator is pure-function; the sweep depends on
real Postgres for cross-table interaction with ops_tickets.
This file covers both — the evaluator's classification logic
gets unit-style tests against in-memory model instances; the
sweep's open/auto-resolve flow runs through the live DB.

DATABASE_URL required for the sweep tests.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.models import (
    CarePlanGoal,
    MetricObservation,
    OpsTicket,
    OpsTicketStatus,
    Patient,
)
from app.db.repositories import (
    care_plan_goals as goals_repo,
    ops_tickets as ops_tickets_repo,
)
from app.db.session import get_sessionmaker
from services.scheduler import goal_drift_sweep

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping drift tests",
)


# ---- pure evaluator tests (no DB) -----------------------------------------


def _make_goal(
    *,
    target: str = "7.0",
    comparator: str = "less_than",
    created_at: datetime | None = None,
) -> CarePlanGoal:
    """Build an in-memory goal for evaluator tests. Bypasses
    the repo because we want to control timestamps + skip the
    DB. Status defaults to ``active`` since drift is only
    evaluated for active goals."""
    g = CarePlanGoal()
    g.id = 1
    g.patient_id = 1
    g.metric_key = "hba1c"
    g.metric_label = "HbA1c"
    g.target_value = Decimal(target)
    g.comparator = comparator
    g.target_unit = "%"
    g.status = "active"
    g.created_at = created_at or datetime.now(timezone.utc)
    g.updated_at = g.created_at
    return g


def _make_obs(
    value: str, *, observed_at: datetime
) -> MetricObservation:
    obs = MetricObservation()
    obs.id = 1
    obs.patient_id = 1
    obs.goal_id = 1
    obs.metric_key = "hba1c"
    obs.value = Decimal(value)
    obs.unit = "%"
    obs.observed_at = observed_at
    obs.source = "manual"
    return obs


def test_drift_no_data_for_freshly_created_goal():
    """No observations + recently-created goal → ``no_data``,
    not ``stale``. We don't want to alert immediately on a
    just-created goal."""
    goal = _make_goal(
        created_at=datetime.now(timezone.utc) - timedelta(days=1)
    )
    assert (
        goals_repo.evaluate_drift_status(goal, [])
        == "no_data"
    )


def test_drift_stale_for_old_goal_with_no_obs():
    goal = _make_goal(
        created_at=datetime.now(timezone.utc) - timedelta(days=30)
    )
    assert (
        goals_repo.evaluate_drift_status(goal, [])
        == "stale"
    )


def test_drift_on_target_when_most_recent_meets_target():
    """Even if older values were off-target, the most recent
    on-target observation means the patient is currently on
    target — no drift alert."""
    now = datetime.now(timezone.utc)
    obs = [
        _make_obs("6.8", observed_at=now - timedelta(days=1)),  # newest, on target
        _make_obs("7.4", observed_at=now - timedelta(days=4)),
        _make_obs("7.5", observed_at=now - timedelta(days=7)),
    ]
    assert (
        goals_repo.evaluate_drift_status(_make_goal(), obs)
        == "on_target"
    )


def test_drift_slipping_when_was_on_target_then_off():
    """The doctor's most concerning case: patient WAS on
    target, but the latest reading is back over the line.
    This is the regression alert — caught even within a
    single-window slip."""
    now = datetime.now(timezone.utc)
    obs = [
        _make_obs("7.3", observed_at=now - timedelta(days=1)),  # newest, off target
        _make_obs("6.8", observed_at=now - timedelta(days=4)),  # was on target
        _make_obs("6.9", observed_at=now - timedelta(days=7)),
    ]
    assert (
        goals_repo.evaluate_drift_status(_make_goal(), obs)
        == "slipping"
    )


def test_drift_persistent_off_when_never_on_target():
    """All recent observations off-target. Different from
    slipping — patient never got there in the window. Doctor
    action is "review the regimen", not "patient backslid"."""
    now = datetime.now(timezone.utc)
    obs = [
        _make_obs("7.4", observed_at=now - timedelta(days=1)),
        _make_obs("7.5", observed_at=now - timedelta(days=4)),
        _make_obs("7.6", observed_at=now - timedelta(days=7)),
    ]
    assert (
        goals_repo.evaluate_drift_status(_make_goal(), obs)
        == "persistent_off"
    )


def test_drift_stale_takes_priority_when_last_obs_too_old():
    """Off-target observations from > 14 days ago classify as
    ``stale``, not ``persistent_off`` — the engagement gap is
    the primary signal, not the off-target value."""
    now = datetime.now(timezone.utc)
    obs = [
        _make_obs("7.4", observed_at=now - timedelta(days=20)),
        _make_obs("7.5", observed_at=now - timedelta(days=25)),
    ]
    assert (
        goals_repo.evaluate_drift_status(_make_goal(), obs)
        == "stale"
    )


def test_drift_greater_than_comparator_inverts_target_check():
    """Goals like 'exercise > 150 min/week' compare in the
    opposite direction. Make sure the evaluator handles both
    comparators consistently."""
    now = datetime.now(timezone.utc)
    goal = _make_goal(target="150", comparator="greater_than")
    # Patient hit target last time, but slipped on the most
    # recent week.
    obs = [
        _make_obs("100", observed_at=now - timedelta(days=1)),  # off target (less than 150)
        _make_obs("180", observed_at=now - timedelta(days=8)),  # on target
    ]
    assert (
        goals_repo.evaluate_drift_status(goal, obs) == "slipping"
    )


def test_meets_consecutive_target():
    now = datetime.now(timezone.utc)
    goal = _make_goal()  # hba1c < 7.0
    on = [
        _make_obs("6.8", observed_at=now - timedelta(days=1)),
        _make_obs("6.7", observed_at=now - timedelta(days=4)),
        _make_obs("6.9", observed_at=now - timedelta(days=7)),
    ]
    assert goals_repo.meets_consecutive_target(goal, on, consecutive=3)
    # One off-target in the window → not achieved.
    mixed = [
        _make_obs("6.8", observed_at=now - timedelta(days=1)),
        _make_obs("7.4", observed_at=now - timedelta(days=4)),
        _make_obs("6.9", observed_at=now - timedelta(days=7)),
    ]
    assert not goals_repo.meets_consecutive_target(goal, mixed, consecutive=3)
    # Fewer than `consecutive` observations → not achieved (no lucky single).
    assert not goals_repo.meets_consecutive_target(goal, on[:2], consecutive=3)


# ---- sweep integration tests ----------------------------------------------


async def _seed_patient_with_goal(
    *,
    target: str = "7.0",
    comparator: str = "less_than",
) -> tuple[int, str, int]:
    """Returns (patient_id, phone, goal_id)."""
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(
            full_name=f"Drift Test {suffix}",
            phone=f"drift-{suffix}",
            consent_sms=True,
        )
        db.add(p)
        await db.flush()
        goal = await goals_repo.create_goal(
            db,
            patient_id=p.id,
            metric_key="hba1c",
            metric_label="HbA1c",
            target_value=Decimal(target),
            comparator=comparator,
            target_unit="%",
            created_by="ops",
        )
        await db.commit()
        return p.id, p.phone, goal.id


async def _seed_observations(
    *, patient_id: int, goal_id: int, values_with_offsets: list[tuple[str, int]]
) -> None:
    """``values_with_offsets`` is [(value, days_ago), ...]."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        for value, days in values_with_offsets:
            await goals_repo.record_observation(
                db,
                patient_id=patient_id,
                goal_id=goal_id,
                metric_key="hba1c",
                value=Decimal(value),
                unit="%",
                observed_at=(
                    datetime.now(timezone.utc)
                    - timedelta(days=days)
                ),
                source="manual",
            )
        await db.commit()


async def test_sweep_opens_ticket_for_slipping_goal():
    pid, phone, gid = await _seed_patient_with_goal()
    await _seed_observations(
        patient_id=pid,
        goal_id=gid,
        values_with_offsets=[("7.3", 1), ("6.8", 4)],  # slipping
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await goal_drift_sweep.sweep_goal_drift(db)
        await db.commit()

    assert result["opened"] >= 1

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        ticket = (
            await ops_tickets_repo.find_open_for_patient_category(
                db,
                patient_id=phone,
                category=f"goal_drift:{gid}",
            )
        )
    assert ticket is not None
    assert ticket.status == OpsTicketStatus.open
    assert "slipping" in (ticket.notes or "")


async def test_sweep_opens_ticket_for_persistent_off_goal():
    pid, phone, gid = await _seed_patient_with_goal()
    await _seed_observations(
        patient_id=pid,
        goal_id=gid,
        values_with_offsets=[
            ("7.4", 1),
            ("7.5", 4),
            ("7.6", 7),
        ],
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await goal_drift_sweep.sweep_goal_drift(db)
        await db.commit()

    assert result["opened"] >= 1

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        ticket = (
            await ops_tickets_repo.find_open_for_patient_category(
                db,
                patient_id=phone,
                category=f"goal_drift:{gid}",
            )
        )
    assert ticket is not None
    assert "persistent_off" in (ticket.notes or "")


async def test_sweep_idempotent_does_not_double_open():
    """Running the sweep twice on the same drifting goal must
    NOT open a second ticket — the per-goal category serves
    as the dedup key."""
    pid, _, gid = await _seed_patient_with_goal()
    await _seed_observations(
        patient_id=pid,
        goal_id=gid,
        values_with_offsets=[("7.3", 1), ("6.8", 4)],
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await goal_drift_sweep.sweep_goal_drift(db)
        await db.commit()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await goal_drift_sweep.sweep_goal_drift(db)
        await db.commit()

    assert result["opened"] == 0


async def test_sweep_auto_resolves_when_back_on_target():
    """First sweep finds the goal slipping → opens a ticket.
    Patient records a new on-target observation. Next sweep
    should auto-resolve the open ticket."""
    pid, phone, gid = await _seed_patient_with_goal()
    await _seed_observations(
        patient_id=pid,
        goal_id=gid,
        values_with_offsets=[("7.3", 1), ("6.8", 4)],
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        first = await goal_drift_sweep.sweep_goal_drift(db)
        await db.commit()
    assert first["opened"] >= 1

    # Patient gets back on target.
    await _seed_observations(
        patient_id=pid,
        goal_id=gid,
        values_with_offsets=[("6.5", 0)],
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        second = await goal_drift_sweep.sweep_goal_drift(db)
        await db.commit()

    assert second["resolved"] >= 1

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        ticket = (
            await ops_tickets_repo.find_open_for_patient_category(
                db,
                patient_id=phone,
                category=f"goal_drift:{gid}",
            )
        )
    # find_open returns only open/acknowledged — resolved
    # tickets are excluded.
    assert ticket is None


async def test_sweep_skips_inactive_goals():
    """``list_all_active_goals`` filters at the DB level; an
    achieved/inactive goal must not appear in the sweep's
    examined set, even if it has off-target observations."""
    pid, _, gid = await _seed_patient_with_goal()
    await _seed_observations(
        patient_id=pid,
        goal_id=gid,
        values_with_offsets=[("7.5", 1)],
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await goals_repo.update_status(
            db, gid, status="achieved"
        )
        await db.commit()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await goal_drift_sweep.sweep_goal_drift(db)
        await db.commit()

    # The achieved goal must not be in the examined set;
    # other tests' goals may be, so we only assert ours
    # didn't open a ticket.
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows = list(
            (
                await db.execute(
                    select(OpsTicket).where(
                        OpsTicket.category
                        == f"goal_drift:{gid}"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows == []


async def test_sweep_does_not_alert_no_data_freshly_created_goal():
    """A goal with no observations + recently created should
    be classified ``no_data`` and NOT open a ticket. Only
    when the goal ages past stale_after_days does silence
    become alert-worthy."""
    _, _, gid = await _seed_patient_with_goal()
    # No observations seeded.

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await goal_drift_sweep.sweep_goal_drift(db)
        await db.commit()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows = list(
            (
                await db.execute(
                    select(OpsTicket).where(
                        OpsTicket.category
                        == f"goal_drift:{gid}"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows == []


async def test_dto_drift_status_in_list_endpoint():
    """The /patients/{id}/goals endpoint includes the
    drift_status field on each goal so the UI can render the
    badge without a separate fetch."""
    from fastapi.testclient import TestClient

    from services.orchestrator.main import app

    pid, _, gid = await _seed_patient_with_goal()
    await _seed_observations(
        patient_id=pid,
        goal_id=gid,
        values_with_offsets=[("7.3", 1), ("6.8", 4)],
    )

    with TestClient(app) as client:
        resp = client.get(f"/patients/{pid}/goals")
    assert resp.status_code == 200
    rows = resp.json()
    target = next(r for r in rows if r["id"] == gid)
    assert target["drift_status"] == "slipping"


async def test_sweep_auto_achieves_goal_on_consecutive_on_target():
    """Three consecutive on-target observations flip the goal active →
    achieved (and resolve any open drift ticket)."""
    pid, phone, gid = await _seed_patient_with_goal()
    await _seed_observations(
        patient_id=pid,
        goal_id=gid,
        values_with_offsets=[("6.6", 1), ("6.7", 4), ("6.8", 7)],
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await goal_drift_sweep.sweep_goal_drift(db)
        await db.commit()
    assert result["achieved"] >= 1

    async with SessionLocal() as db:
        goal = await goals_repo.get_goal(db, gid)
    assert goal.status == "achieved"


async def test_sweep_auto_achieve_resolves_open_drift_ticket():
    """A goal that was drifting (ticket open) then strings together N
    on-target readings is achieved AND its drift ticket auto-resolves."""
    pid, phone, gid = await _seed_patient_with_goal()
    # First: a persistent-off history opens a drift ticket.
    await _seed_observations(
        patient_id=pid,
        goal_id=gid,
        values_with_offsets=[("7.4", 20), ("7.5", 23), ("7.6", 26)],
    )
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await goal_drift_sweep.sweep_goal_drift(db)
        await db.commit()
    async with SessionLocal() as db:
        ticket = await ops_tickets_repo.find_open_for_patient_category(
            db, patient_id=phone, category=f"goal_drift:{gid}"
        )
    assert ticket is not None  # drift ticket opened

    # Then: three fresh on-target readings → achieve + resolve the ticket.
    await _seed_observations(
        patient_id=pid,
        goal_id=gid,
        values_with_offsets=[("6.6", 0), ("6.7", 1), ("6.8", 2)],
    )
    async with SessionLocal() as db:
        result = await goal_drift_sweep.sweep_goal_drift(db)
        await db.commit()
    assert result["achieved"] >= 1

    async with SessionLocal() as db:
        goal = await goals_repo.get_goal(db, gid)
        ticket_after = await ops_tickets_repo.find_open_for_patient_category(
            db, patient_id=phone, category=f"goal_drift:{gid}"
        )
    assert goal.status == "achieved"
    assert ticket_after is None  # resolved

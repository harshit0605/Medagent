"""Integration tests for care plan goals + observations
(slice 14).

End-to-end against real Postgres because:
    1. NUMERIC(8,3) round-trip behaviour is dialect-sensitive
       (SQLite drops precision differently than Postgres).
    2. Status transitions rely on Postgres ``onupdate=now()``
       firing on UPDATE — we want to verify the actual
       behaviour, not a mock.
    3. The patient-FK CASCADE on goals + the SET-NULL on
       observation.goal_id are exactly what we want to
       exercise on the real schema.

DATABASE_URL required.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import (
    CarePlanGoal,
    MetricObservation,
    Patient,
)
from app.db.repositories import (
    care_plan_goals as goals_repo,
)
from app.db.session import get_sessionmaker

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping goal tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


# ---- helpers --------------------------------------------------------------


async def _seed_patient() -> tuple[int, str]:
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(
            full_name=f"Goal Test {suffix}",
            phone=f"goal-{suffix}",
            consent_sms=True,
        )
        db.add(p)
        await db.flush()
        await db.commit()
        return p.id, p.phone


# ---- repo / service tests ------------------------------------------------


async def test_create_goal_persists_with_defaults():
    pid, _ = await _seed_patient()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        row = await goals_repo.create_goal(
            db,
            patient_id=pid,
            metric_key="hba1c",
            metric_label="HbA1c",
            target_value=Decimal("7.0"),
            comparator="less_than",
            target_unit="%",
            created_by="dr.test",
            notes="diabetes target",
        )
        await db.commit()

    assert row.id is not None
    assert row.status == "active"
    assert row.target_value == Decimal("7.000")
    assert row.target_unit == "%"
    assert row.comparator == "less_than"


async def test_create_goal_rejects_unsupported_comparator():
    pid, _ = await _seed_patient()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        with pytest.raises(ValueError, match="comparator"):
            await goals_repo.create_goal(
                db,
                patient_id=pid,
                metric_key="bp",
                metric_label="BP",
                target_value=Decimal("130"),
                comparator="between",  # not yet supported in v1
                target_unit="mmHg",
                created_by="dr.test",
            )


async def test_create_goal_rejects_empty_metric_key():
    pid, _ = await _seed_patient()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        with pytest.raises(ValueError, match="metric_key"):
            await goals_repo.create_goal(
                db,
                patient_id=pid,
                metric_key="   ",
                metric_label="x",
                target_value=Decimal("1"),
                comparator="less_than",
                target_unit="x",
                created_by="dr.test",
            )


async def test_record_observation_persists():
    pid, _ = await _seed_patient()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        goal = await goals_repo.create_goal(
            db,
            patient_id=pid,
            metric_key="hba1c",
            metric_label="HbA1c",
            target_value=Decimal("7.0"),
            comparator="less_than",
            target_unit="%",
            created_by="dr.test",
        )
        obs = await goals_repo.record_observation(
            db,
            patient_id=pid,
            goal_id=goal.id,
            metric_key="hba1c",
            value=Decimal("7.3"),
            unit="%",
            source="manual",
            recorded_by="ops",
        )
        await db.commit()

    assert obs.id is not None
    assert obs.value == Decimal("7.300")
    assert obs.goal_id == goal.id
    assert obs.observed_at is not None


async def test_record_observation_rejects_unknown_source():
    pid, _ = await _seed_patient()
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        with pytest.raises(ValueError, match="source"):
            await goals_repo.record_observation(
                db,
                patient_id=pid,
                goal_id=None,
                metric_key="hba1c",
                value=Decimal("7"),
                unit="%",
                source="bogus",
            )


async def test_record_observation_backdates_observed_at():
    """Lab results entered yesterday → observed_at must be
    yesterday's datetime, not now. Trend chart sorts by
    observed_at."""
    pid, _ = await _seed_patient()
    backdate = datetime.now(timezone.utc) - timedelta(days=2)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        obs = await goals_repo.record_observation(
            db,
            patient_id=pid,
            goal_id=None,
            metric_key="weight_kg",
            value=Decimal("78"),
            unit="kg",
            observed_at=backdate,
            source="lab",
        )
        await db.commit()

    diff = abs((obs.observed_at - backdate).total_seconds())
    assert diff < 1.0


async def test_status_transition_active_to_achieved():
    pid, _ = await _seed_patient()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        goal = await goals_repo.create_goal(
            db,
            patient_id=pid,
            metric_key="hba1c",
            metric_label="HbA1c",
            target_value=Decimal("7.0"),
            comparator="less_than",
            target_unit="%",
            created_by="dr.test",
        )
        await db.commit()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        updated = await goals_repo.update_status(
            db, goal.id, status="achieved"
        )
        await db.commit()

    assert updated is not None
    assert updated.status == "achieved"


async def test_status_transition_no_op_skips_update():
    """Setting a goal to its current status should not
    update_at — no-op semantics keep the audit clean."""
    pid, _ = await _seed_patient()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        goal = await goals_repo.create_goal(
            db,
            patient_id=pid,
            metric_key="hba1c",
            metric_label="HbA1c",
            target_value=Decimal("7.0"),
            comparator="less_than",
            target_unit="%",
            created_by="dr.test",
        )
        await db.commit()
        original_updated = goal.updated_at

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        same = await goals_repo.update_status(
            db, goal.id, status="active"
        )
        await db.commit()

    assert same is not None
    assert same.updated_at == original_updated


async def test_list_goals_filters_by_status():
    pid, _ = await _seed_patient()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        a = await goals_repo.create_goal(
            db,
            patient_id=pid,
            metric_key="hba1c",
            metric_label="HbA1c",
            target_value=Decimal("7.0"),
            comparator="less_than",
            target_unit="%",
            created_by="dr.test",
        )
        b = await goals_repo.create_goal(
            db,
            patient_id=pid,
            metric_key="weight_kg",
            metric_label="Weight",
            target_value=Decimal("80"),
            comparator="less_than",
            target_unit="kg",
            created_by="dr.test",
        )
        await goals_repo.update_status(
            db, b.id, status="achieved"
        )
        await db.commit()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        active = await goals_repo.list_goals_for_patient(
            db, pid, status="active"
        )
        achieved = await goals_repo.list_goals_for_patient(
            db, pid, status="achieved"
        )

    assert any(g.id == a.id for g in active)
    assert not any(g.id == b.id for g in active)
    assert any(g.id == b.id for g in achieved)


async def test_list_goals_unknown_status_raises():
    pid, _ = await _seed_patient()
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        with pytest.raises(ValueError, match="status"):
            await goals_repo.list_goals_for_patient(
                db, pid, status="bogus"
            )


async def test_observations_survive_goal_status_change():
    """Marking a goal achieved must NOT delete its
    observations — historical data stays for audit."""
    pid, _ = await _seed_patient()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        goal = await goals_repo.create_goal(
            db,
            patient_id=pid,
            metric_key="hba1c",
            metric_label="HbA1c",
            target_value=Decimal("7.0"),
            comparator="less_than",
            target_unit="%",
            created_by="dr.test",
        )
        await goals_repo.record_observation(
            db,
            patient_id=pid,
            goal_id=goal.id,
            metric_key="hba1c",
            value=Decimal("6.8"),
            unit="%",
            source="lab",
        )
        await goals_repo.update_status(
            db, goal.id, status="achieved"
        )
        await db.commit()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        obs = await goals_repo.list_observations_for_goal(
            db, goal.id
        )

    assert len(obs) == 1
    assert obs[0].value == Decimal("6.800")


# ---- endpoint tests -------------------------------------------------------


async def test_endpoint_create_goal_round_trip(orchestrator_client):
    pid, _ = await _seed_patient()
    response = orchestrator_client.post(
        f"/patients/{pid}/goals",
        json={
            "metric_key": "hba1c",
            "metric_label": "HbA1c",
            "target_value": 7.0,
            "comparator": "less_than",
            "target_unit": "%",
            "created_by": "dr.endpoint",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["patient_id"] == pid
    assert body["status"] == "active"
    assert body["target_value"] == 7.0
    # No observations yet → on_target is null, latest_value is null.
    assert body["latest_value"] is None
    assert body["on_target"] is None


async def test_endpoint_create_goal_404_for_missing_patient(
    orchestrator_client,
):
    response = orchestrator_client.post(
        "/patients/999999999/goals",
        json={
            "metric_key": "hba1c",
            "metric_label": "HbA1c",
            "target_value": 7.0,
            "comparator": "less_than",
            "target_unit": "%",
        },
    )
    assert response.status_code == 404


async def test_endpoint_create_goal_validates_comparator(
    orchestrator_client,
):
    pid, _ = await _seed_patient()
    response = orchestrator_client.post(
        f"/patients/{pid}/goals",
        json={
            "metric_key": "hba1c",
            "metric_label": "HbA1c",
            "target_value": 7.0,
            "comparator": "between",
            "target_unit": "%",
        },
    )
    # Pydantic Literal catches it as 422 before our 400.
    assert response.status_code in (400, 422)


async def test_endpoint_record_observation_inherits_metric_key(
    orchestrator_client,
):
    """The observation's metric_key is taken from the goal,
    not from the request body — operator can't accidentally
    log mmHg against an HbA1c goal."""
    pid, _ = await _seed_patient()
    goal_resp = orchestrator_client.post(
        f"/patients/{pid}/goals",
        json={
            "metric_key": "bp_systolic",
            "metric_label": "BP systolic",
            "target_value": 130,
            "comparator": "less_than",
            "target_unit": "mmHg",
        },
    )
    gid = goal_resp.json()["id"]

    obs_resp = orchestrator_client.post(
        f"/patients/{pid}/goals/{gid}/observations",
        json={"value": 128, "unit": "mmHg"},
    )
    assert obs_resp.status_code == 200
    body = obs_resp.json()
    assert body["metric_key"] == "bp_systolic"
    assert body["goal_id"] == gid


async def test_endpoint_list_goals_includes_latest_observation(
    orchestrator_client,
):
    """The list endpoint pre-fetches the latest observation
    so the UI doesn't N+1. on_target is computed server-side."""
    pid, _ = await _seed_patient()
    goal_resp = orchestrator_client.post(
        f"/patients/{pid}/goals",
        json={
            "metric_key": "hba1c",
            "metric_label": "HbA1c",
            "target_value": 7.0,
            "comparator": "less_than",
            "target_unit": "%",
        },
    )
    gid = goal_resp.json()["id"]

    orchestrator_client.post(
        f"/patients/{pid}/goals/{gid}/observations",
        json={"value": 6.8, "unit": "%"},
    )

    list_resp = orchestrator_client.get(f"/patients/{pid}/goals")
    rows = list_resp.json()
    target = next((r for r in rows if r["id"] == gid), None)
    assert target is not None
    assert target["latest_value"] == 6.8
    assert target["on_target"] is True


async def test_endpoint_list_goals_off_target_when_above_threshold(
    orchestrator_client,
):
    """Latest value 7.5 against ``hba1c < 7`` → on_target =
    False."""
    pid, _ = await _seed_patient()
    goal_resp = orchestrator_client.post(
        f"/patients/{pid}/goals",
        json={
            "metric_key": "hba1c",
            "metric_label": "HbA1c",
            "target_value": 7.0,
            "comparator": "less_than",
            "target_unit": "%",
        },
    )
    gid = goal_resp.json()["id"]
    orchestrator_client.post(
        f"/patients/{pid}/goals/{gid}/observations",
        json={"value": 7.5, "unit": "%"},
    )

    rows = orchestrator_client.get(
        f"/patients/{pid}/goals"
    ).json()
    target = next((r for r in rows if r["id"] == gid), None)
    assert target is not None
    assert target["on_target"] is False


async def test_endpoint_status_404_for_other_patients_goal(
    orchestrator_client,
):
    """Patching a goal that belongs to a different patient
    returns 404 — defends against ID guessing across
    patients."""
    pid_a, _ = await _seed_patient()
    pid_b, _ = await _seed_patient()
    goal_resp = orchestrator_client.post(
        f"/patients/{pid_a}/goals",
        json={
            "metric_key": "hba1c",
            "metric_label": "HbA1c",
            "target_value": 7.0,
            "comparator": "less_than",
            "target_unit": "%",
        },
    )
    gid = goal_resp.json()["id"]

    response = orchestrator_client.patch(
        f"/patients/{pid_b}/goals/{gid}/status",
        json={"status": "achieved"},
    )
    assert response.status_code == 404


async def test_endpoint_observations_404_when_goal_for_other_patient(
    orchestrator_client,
):
    pid_a, _ = await _seed_patient()
    pid_b, _ = await _seed_patient()
    goal_resp = orchestrator_client.post(
        f"/patients/{pid_a}/goals",
        json={
            "metric_key": "hba1c",
            "metric_label": "HbA1c",
            "target_value": 7.0,
            "comparator": "less_than",
            "target_unit": "%",
        },
    )
    gid = goal_resp.json()["id"]

    response = orchestrator_client.post(
        f"/patients/{pid_b}/goals/{gid}/observations",
        json={"value": 7.5, "unit": "%"},
    )
    assert response.status_code == 404


async def test_endpoint_list_observations_returns_newest_first(
    orchestrator_client,
):
    pid, _ = await _seed_patient()
    goal_resp = orchestrator_client.post(
        f"/patients/{pid}/goals",
        json={
            "metric_key": "weight_kg",
            "metric_label": "Weight",
            "target_value": 80,
            "comparator": "less_than",
            "target_unit": "kg",
        },
    )
    gid = goal_resp.json()["id"]

    older = (
        datetime.now(timezone.utc) - timedelta(days=2)
    ).isoformat()
    newer = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).isoformat()
    orchestrator_client.post(
        f"/patients/{pid}/goals/{gid}/observations",
        json={"value": 79.5, "unit": "kg", "observed_at": older},
    )
    orchestrator_client.post(
        f"/patients/{pid}/goals/{gid}/observations",
        json={"value": 79.0, "unit": "kg", "observed_at": newer},
    )

    rows = orchestrator_client.get(
        f"/patients/{pid}/goals/{gid}/observations"
    ).json()
    assert len(rows) >= 2
    assert rows[0]["observed_at"] >= rows[1]["observed_at"]


async def test_observation_orphaned_on_goal_delete():
    """Deleting a goal SET-NULLs the observation.goal_id but
    keeps the observation row. (We don't expose hard delete
    in the API yet — verify the FK behaviour at the DB
    level for when we do.)"""
    pid, _ = await _seed_patient()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        goal = await goals_repo.create_goal(
            db,
            patient_id=pid,
            metric_key="hba1c",
            metric_label="HbA1c",
            target_value=Decimal("7.0"),
            comparator="less_than",
            target_unit="%",
            created_by="dr.test",
        )
        obs = await goals_repo.record_observation(
            db,
            patient_id=pid,
            goal_id=goal.id,
            metric_key="hba1c",
            value=Decimal("6.9"),
            unit="%",
            source="lab",
        )
        await db.commit()
        obs_id = obs.id
        goal_id = goal.id

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await db.delete(await db.get(CarePlanGoal, goal_id))
        await db.commit()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows = list(
            (
                await db.execute(
                    select(MetricObservation).where(
                        MetricObservation.id == obs_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].goal_id is None

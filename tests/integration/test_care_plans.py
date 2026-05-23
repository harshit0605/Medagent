"""Integration tests for care_plans CRUD + endpoints + cohort allowlist."""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_sessionmaker
from app.db.repositories import care_plans as care_plans_repo

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping care_plans integration tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


def _unique_test_name() -> str:
    return f"Test {uuid.uuid4().hex[:8]}"


# ---- Repo --------------------------------------------------------------


async def test_seed_data_includes_v1_plans():
    """Migration 0014 seeds the 3 hard-coded V1 rules. Verify they exist
    so a fresh DB matches the original sweep behaviour."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        plans = await care_plans_repo.list_active(db)
    test_names = {p.test_name for p in plans}
    assert "HbA1c" in test_names
    assert "Blood pressure check" in test_names
    assert "Vitamin D level" in test_names


async def test_create_and_deactivate_plan():
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        plan = await care_plans_repo.create(
            db,
            cohort_attr="cohort_diabetes",
            test_name=_unique_test_name(),
            cadence_days=120,
            due_in_days=10,
            notes="test plan",
            created_by="test",
        )
        await db.commit()
        plan_id = plan.id

        deactivated = await care_plans_repo.deactivate(db, plan_id)
        await db.commit()
    assert deactivated is not None
    assert deactivated.active is False

    # Active list excludes the deactivated row; full list still includes it.
    async with SessionLocal() as db:
        active = await care_plans_repo.list_active(db)
        all_plans = await care_plans_repo.list_all(db)
    assert all(p.id != plan_id for p in active)
    assert any(p.id == plan_id for p in all_plans)


async def test_update_partial_fields():
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        plan = await care_plans_repo.create(
            db,
            cohort_attr="cohort_cardiac",
            test_name=_unique_test_name(),
            cadence_days=90,
            due_in_days=14,
        )
        await db.commit()
        plan_id = plan.id

        updated = await care_plans_repo.update(
            db, plan_id, cadence_days=60, notes="tighter schedule"
        )
        await db.commit()
    assert updated.cadence_days == 60
    assert updated.notes == "tighter schedule"
    # Untouched fields preserved.
    assert updated.due_in_days == 14
    assert updated.active is True


# ---- Endpoints ---------------------------------------------------------


def test_endpoint_list_includes_seed_plans(orchestrator_client):
    r = orchestrator_client.get("/care-plans")
    assert r.status_code == 200
    body = r.json()
    test_names = {p["test_name"] for p in body}
    assert {"HbA1c", "Blood pressure check", "Vitamin D level"} <= test_names


def test_endpoint_cohorts_returns_allowlist(orchestrator_client):
    """Phase 3b changed the response shape from a flat list[str] of
    cohort_attrs to a list of {kind, cohort_attr, cohort_tag_id, label,
    description} options so the picker can show legacy cohorts +
    clinician-authored tags side by side."""
    r = orchestrator_client.get("/care-plans/cohorts")
    assert r.status_code == 200
    options = r.json()
    boolean_attrs = {
        opt["cohort_attr"] for opt in options if opt["kind"] == "boolean"
    }
    assert {"cohort_diabetes", "cohort_cardiac", "cohort_fall_risk"} <= boolean_attrs


def test_endpoint_create_rejects_unknown_cohort(orchestrator_client):
    r = orchestrator_client.post(
        "/care-plans",
        json={
            "cohort_attr": "cohort_made_up",
            "test_name": _unique_test_name(),
            "cadence_days": 90,
        },
    )
    assert r.status_code == 400
    assert "unknown cohort" in r.json()["detail"].lower()


def test_endpoint_create_rejects_duplicate(orchestrator_client):
    """The first create succeeds; the second with the same (cohort, test)
    must 409 instead of letting the unique constraint surface as a 500."""
    test_name = _unique_test_name()
    first = orchestrator_client.post(
        "/care-plans",
        json={
            "cohort_attr": "cohort_diabetes",
            "test_name": test_name,
            "cadence_days": 90,
        },
    )
    assert first.status_code == 200

    second = orchestrator_client.post(
        "/care-plans",
        json={
            "cohort_attr": "cohort_diabetes",
            "test_name": test_name,
            "cadence_days": 90,
        },
    )
    assert second.status_code == 409


def test_endpoint_update_then_deactivate_lifecycle(orchestrator_client):
    create = orchestrator_client.post(
        "/care-plans",
        json={
            "cohort_attr": "cohort_cardiac",
            "test_name": _unique_test_name(),
            "cadence_days": 120,
            "due_in_days": 14,
        },
    )
    assert create.status_code == 200
    plan = create.json()
    plan_id = plan["id"]

    upd = orchestrator_client.put(
        f"/care-plans/{plan_id}",
        json={"cadence_days": 60, "notes": "tightened"},
    )
    assert upd.status_code == 200
    assert upd.json()["cadence_days"] == 60
    assert upd.json()["notes"] == "tightened"

    de = orchestrator_client.post(f"/care-plans/{plan_id}/deactivate")
    assert de.status_code == 200
    assert de.json()["active"] is False

    # Active-only list excludes; include_inactive=true includes.
    active = orchestrator_client.get("/care-plans").json()
    inactive = orchestrator_client.get(
        "/care-plans", params={"include_inactive": "true"}
    ).json()
    assert all(p["id"] != plan_id for p in active)
    assert any(p["id"] == plan_id for p in inactive)


def test_endpoint_404_on_unknown_plan(orchestrator_client):
    assert (
        orchestrator_client.put(
            "/care-plans/9999999", json={"cadence_days": 30}
        ).status_code
        == 404
    )
    assert (
        orchestrator_client.post(
            "/care-plans/9999999/deactivate"
        ).status_code
        == 404
    )


# ---- Sweep + DB plans interaction ---------------------------------------


async def test_sweep_uses_dynamic_plans(orchestrator_client):
    """A new active plan added via the API should be picked up by the
    sweep — that's the whole point of moving plans out of constants."""
    from services.scheduler import care_gaps

    test_name = _unique_test_name()
    create = orchestrator_client.post(
        "/care-plans",
        json={
            "cohort_attr": "cohort_diabetes",
            "test_name": test_name,
            "cadence_days": 30,
        },
    )
    assert create.status_code == 200

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        plans = await care_gaps.load_active_plans(db)
    assert any(
        p.cohort_attr == "cohort_diabetes" and p.test_name == test_name
        for p in plans
    )

"""Integration tests for the vitals self-report flow (DB-backed).

Covers persistence (metric_observation written with source=
patient_self_report), goal linkage (matched to an active care_plan_goal
by metric_key), and the end-to-end /route path (sync-fallback router
dispatches a reading to the vitals handler; a symptom+reading routes to
safety, not vitals).

Skipped when DATABASE_URL is unset.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.db.models import Patient
from app.db.repositories import care_plan_goals as goals_repo
from app.db.session import get_sessionmaker
from services.orchestrator import vitals_handler

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping vitals integration tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


async def _seed_patient() -> tuple[int, str]:
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(
            full_name=f"Vitals Test {suffix}",
            phone=f"vitals-{suffix}",
            consent_sms=True,
            cohort_diabetes=True,
        )
        db.add(p)
        await db.flush()
        await db.commit()
        return p.id, p.phone


async def _observations(patient_id: int, metric_key: str) -> list:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows = await goals_repo.list_observations_for_patient(
            db, patient_id, metric_key=metric_key
        )
    return rows


# ---- handler-level tests ---------------------------------------------------


async def test_handle_vitals_persists_observation_no_goal():
    pid, phone = await _seed_patient()
    delta = await vitals_handler.handle_vitals_log(
        patient_phone=phone, new_user_text="sugar 140"
    )
    assert delta is not None
    assert "Logged" in delta["response_body"]
    assert "vitals_self_report" in delta["audit_reasons"]

    rows = await _observations(pid, "blood_glucose")
    assert len(rows) == 1
    assert rows[0].value == Decimal("140.000")
    assert rows[0].source == "patient_self_report"
    assert rows[0].goal_id is None  # no active goal to link


async def test_handle_vitals_links_matching_goal_and_notes_target():
    pid, phone = await _seed_patient()
    # Doctor sets a glucose goal.
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        goal = await goals_repo.create_goal(
            db,
            patient_id=pid,
            metric_key="blood_glucose",
            metric_label="Blood glucose",
            target_value=Decimal("130"),
            comparator="less_than",
            target_unit="mg/dL",
            created_by="ops",
        )
        await db.commit()
        goal_id = goal.id

    # Patient reports an off-target reading.
    delta = await vitals_handler.handle_vitals_log(
        patient_phone=phone, new_user_text="my sugar is 180"
    )
    assert delta is not None
    # Off-target (180 > target 130) — ack should nudge, not congratulate.
    assert "130" in delta["response_body"]

    rows = await _observations(pid, "blood_glucose")
    assert len(rows) == 1
    assert rows[0].goal_id == goal_id  # linked to the active goal
    assert rows[0].value == Decimal("180.000")


async def test_handle_vitals_bp_writes_two_observations():
    pid, phone = await _seed_patient()
    delta = await vitals_handler.handle_vitals_log(
        patient_phone=phone, new_user_text="BP 130/85"
    )
    assert delta is not None
    sys_rows = await _observations(pid, "bp_systolic")
    dia_rows = await _observations(pid, "bp_diastolic")
    assert len(sys_rows) == 1 and sys_rows[0].value == Decimal("130.000")
    assert len(dia_rows) == 1 and dia_rows[0].value == Decimal("85.000")


async def test_handle_vitals_unknown_patient_returns_none():
    delta = await vitals_handler.handle_vitals_log(
        patient_phone="vitals-nobody-xyz", new_user_text="sugar 140"
    )
    assert delta is None


async def test_handle_vitals_no_reading_returns_none():
    _, phone = await _seed_patient()
    delta = await vitals_handler.handle_vitals_log(
        patient_phone=phone, new_user_text="thanks doctor"
    )
    assert delta is None


# ---- /route end-to-end (sync fallback path) --------------------------------


async def test_route_logs_vitals_end_to_end(orchestrator_client):
    pid, phone = await _seed_patient()
    resp = orchestrator_client.post(
        "/route",
        json={
            "message": {
                "patient_id": phone,
                "text": "sugar 155",
                "input_kind": "text",
                "message_id": f"wamid-{uuid.uuid4().hex[:8]}",
            },
        },
    )
    assert resp.status_code == 200

    rows = await _observations(pid, "blood_glucose")
    assert len(rows) == 1
    assert rows[0].value == Decimal("155.000")
    assert rows[0].source == "patient_self_report"


async def test_route_symptom_plus_reading_skips_vitals_for_safety(
    orchestrator_client,
):
    """'sugar 400 and I feel dizzy' is a clinical signal first. The vitals
    short-circuit must defer to the safety path, so NO observation is
    written (the message routes to triage instead)."""
    pid, phone = await _seed_patient()
    resp = orchestrator_client.post(
        "/route",
        json={
            "message": {
                "patient_id": phone,
                "text": "sugar 400 and I have a side effect, feeling dizzy",
                "input_kind": "text",
                "message_id": f"wamid-{uuid.uuid4().hex[:8]}",
            },
        },
    )
    assert resp.status_code == 200
    rows = await _observations(pid, "blood_glucose")
    assert rows == []  # safety path took over; no silent vitals log

"""End-to-end coverage for the compiled LangGraph /route path.

Every other integration test runs with LANGGRAPH_ENABLED=0, exercising the
sync-fallback runner. This file builds the REAL compiled graph in-memory
(checkpointer=None — no Postgres checkpointer needed) and forces /route onto it
by patching ``_get_graph``, then asserts the graph routes the same flows as the
sync path. ``runner == "langgraph"`` in the response proves the compiled path
actually ran.

Skipped when DATABASE_URL is unset or langgraph isn't installed.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.db.models import Patient
from app.db.repositories import care_plan_goals as goals_repo
from app.db.repositories import orders as orders_repo
from app.db.repositories import regimens as regimens_repo
from app.db.session import get_sessionmaker
from services.orchestrator import main as orch_main
from services.orchestrator.agent_workflow import build_langgraph_workflow

_GRAPH = build_langgraph_workflow(checkpointer=None)

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL") or _GRAPH is None,
    reason="DATABASE_URL unset or langgraph unavailable",
)


@pytest.fixture()
def graph_client(monkeypatch):
    """A /route client forced onto the compiled-graph path."""
    monkeypatch.setattr(orch_main, "_get_graph", lambda _request: _GRAPH)
    with TestClient(orch_main.app) as client:
        yield client


async def _seed_patient(*, cohort_diabetes: bool = False) -> tuple[int, str]:
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(
            full_name=f"Graph Test {suffix}",
            phone=f"graph-{suffix}",
            consent_sms=True,
            cohort_diabetes=cohort_diabetes,
        )
        db.add(p)
        await db.flush()
        await db.commit()
        return p.id, p.phone


def _post(client, phone: str, text: str) -> dict:
    resp = client.post(
        "/route",
        json={
            "message": {
                "patient_id": phone,
                "text": text,
                "message_id": f"wamid-{uuid.uuid4().hex[:8]}",
            }
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_graph_path_is_actually_used(graph_client):
    _pid, phone = await _seed_patient()
    body = _post(graph_client, phone, "hello there")
    # Proves the compiled graph ran (not the sync fallback).
    assert body["runner"] == "langgraph"


async def test_graph_path_routes_vitals_to_observation(graph_client):
    pid, phone = await _seed_patient(cohort_diabetes=True)
    body = _post(graph_client, phone, "sugar 155")
    assert body["runner"] == "langgraph"

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows = await goals_repo.list_observations_for_patient(
            db, pid, metric_key="blood_glucose"
        )
    assert len(rows) == 1
    assert rows[0].value == Decimal("155.000")
    assert rows[0].source == "patient_self_report"


async def test_graph_path_routes_order_substitution(graph_client):
    pid, phone = await _seed_patient()
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        regimen = await regimens_repo.create(
            db,
            patient_id=pid,
            medication_name="Atorvastatin",
            dose="10 mg",
            schedule={},
        )
        order = await orders_repo.create(
            db,
            patient_id=pid,
            regimen_id=regimen.id,
            medication_name="Atorvastatin",
            dose="10 mg",
            partner="acme_rx",
        )
        await orders_repo.propose_substitution(
            db, order.id, medication="Atorvastatin (Brand B)"
        )
        await db.commit()
        order_id = order.id

    body = _post(
        graph_client, phone, f"[order-action] sub_approve order_id={order_id}"
    )
    assert body["runner"] == "langgraph"

    async with SessionLocal() as db:
        order = await orders_repo.get(db, order_id)
    assert order.substitution_status == "approved"


async def test_graph_path_general_question_composes(graph_client):
    _pid, phone = await _seed_patient()
    body = _post(graph_client, phone, "what should I do about my appointment?")
    assert body["runner"] == "langgraph"
    # Reaches the compose path (intent classified, a reply produced).
    assert body["message_out"]["body"]

"""Integration tests for the compiled LangGraph workflow.

Skipped when DATABASE_URL is unset. These tests bring up the full FastAPI
lifespan (which constructs a ConnectionPool, PostgresSaver, and compiled
graph), then exercise it via TestClient.

The LLM is disabled here so the assertions remain deterministic; the
LLM integration tests live in tests/integration/test_llm_workflow.py.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select


pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping integration tests",
)


@pytest.fixture(scope="module")
def langgraph_orchestrator_client():
    """Bring the orchestrator app up with LANGGRAPH_ENABLED=1 for this module."""
    prior_lg = os.environ.get("LANGGRAPH_ENABLED")
    prior_llm = os.environ.get("LLM_ENABLED")
    os.environ["LANGGRAPH_ENABLED"] = "1"
    os.environ["LLM_ENABLED"] = "0"  # keep deterministic outputs

    # Force a fresh import so the new env values take effect.
    import importlib
    import services.orchestrator.main as orch_main

    orch_main = importlib.reload(orch_main)

    with TestClient(orch_main.app) as client:
        # Sanity check: lifespan should have wired a real graph.
        assert client.app.state.graph is not None, (
            "compiled graph wasn't created — check DATABASE_URL and PostgresSaver setup"
        )
        yield client

    if prior_lg is None:
        os.environ.pop("LANGGRAPH_ENABLED", None)
    else:
        os.environ["LANGGRAPH_ENABLED"] = prior_lg
    if prior_llm is None:
        os.environ.pop("LLM_ENABLED", None)
    else:
        os.environ["LLM_ENABLED"] = prior_llm


def test_compiled_graph_runs_via_route(langgraph_orchestrator_client):
    # Use refill-keyword phrasing so the rule-based intent classifier hits;
    # the LLM is disabled in this fixture to keep assertions deterministic.
    patient_id = f"lg-{uuid.uuid4().hex[:10]}"
    response = langgraph_orchestrator_client.post(
        "/route",
        json={
            "message": {
                "message_id": f"msg-{uuid.uuid4().hex[:8]}",
                "patient_id": patient_id,
                "phone": "+10000000000",
                "text": "Need a refill",
            }
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["runner"] == "langgraph"
    assert body["intent"] == "refill_request"
    assert body["escalation_required"] is False
    assert body["ticket_id"] is None


async def test_critical_path_creates_ops_ticket_via_human_handoff(langgraph_orchestrator_client):
    """The conditional edge sends critical messages through human_handoff,
    which creates an ops_ticket synchronously."""
    patient_id = f"lg-{uuid.uuid4().hex[:10]}"
    response = langgraph_orchestrator_client.post(
        "/route",
        json={
            "message": {
                "message_id": f"msg-{uuid.uuid4().hex[:8]}",
                "patient_id": patient_id,
                "phone": "+10000000000",
                "text": "I have chest pain",
            }
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["runner"] == "langgraph"
    assert body["risk_level"] == "critical"
    assert body["escalation_required"] is True
    ticket_id = body["ticket_id"]
    assert ticket_id is not None, "human_handoff should have produced a ticket"

    # Verify the ticket is on the ops queue, with the right priority/SLA.
    from app.db.models import OpsTicket
    from app.db.session import get_sessionmaker

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        ticket = (
            await db.execute(
                select(OpsTicket).where(OpsTicket.id == int(ticket_id))
            )
        ).scalar_one()
        assert ticket.patient_id == patient_id
        assert ticket.priority == "p0"
        assert ticket.sla_minutes == 5
        assert ticket.category == "agent_escalation"
        assert ticket.status.value == "open"


async def test_checkpoint_tables_exist(langgraph_orchestrator_client):
    """``AsyncPostgresSaver.setup()`` must have created the checkpoint_* tables on
    first lifespan boot. Re-running setup is idempotent."""
    from sqlalchemy import inspect

    from app.db.session import get_engine

    async with get_engine().connect() as conn:
        tables = set(
            await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names(schema="public"))
        )
    expected = {"checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"}
    missing = expected - tables
    assert not missing, f"missing checkpoint tables: {missing}"


def test_non_critical_path_skips_human_handoff(langgraph_orchestrator_client):
    """Confirm the conditional edge actually branches — non-critical inputs
    must NOT create a ticket."""
    patient_id = f"lg-{uuid.uuid4().hex[:10]}"
    response = langgraph_orchestrator_client.post(
        "/route",
        json={
            "message": {
                "message_id": f"msg-{uuid.uuid4().hex[:8]}",
                "patient_id": patient_id,
                "phone": "+10000000000",
                "text": "Lab booked",
            }
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ticket_id"] is None
    assert body["escalation_required"] is False


async def test_policy_gate_writes_policy_decision_audit_row(langgraph_orchestrator_client):
    """The policy node uses DbAuditTrail; that should produce a
    record_type='policy_decision' row alongside the workflow_summary written
    by /route."""
    patient_id = f"lg-{uuid.uuid4().hex[:10]}"
    response = langgraph_orchestrator_client.post(
        "/route",
        json={
            "message": {
                "message_id": f"msg-{uuid.uuid4().hex[:8]}",
                "patient_id": patient_id,
                "phone": "+10000000000",
                "text": "Lab booked",
            }
        },
    )
    assert response.status_code == 200
    body = response.json()
    # Structured PolicyGate output should be threaded through to the response.
    assert body["flow_action"] == "ALLOW"
    assert any(
        code.startswith("TEMPLATE_REQUIRED") or code.startswith("FREEFORM_ALLOWED")
        for code in body["policy_reason_codes"]
    )
    assert "HUMAN_ESCALATION_EXPOSED" in body["policy_reason_codes"]

    from app.db.models import AuditRecord
    from app.db.session import get_sessionmaker

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(AuditRecord).where(AuditRecord.patient_id == patient_id)
            )
        ).scalars().all()
        types = {row.record_type for row in rows}
        assert {"policy_decision", "workflow_summary"} <= types

        policy_row = next(r for r in rows if r.record_type == "policy_decision")
        assert policy_row.flow_action == "ALLOW"
        # PolicyGate's reasons should NOT include the workflow-only escalation
        # info (escalation hasn't been computed yet at the policy node).
        assert "HUMAN_ESCALATION_EXPOSED" in policy_row.reason_codes


async def test_policy_gate_rejects_disallowed_medicine_flow():
    """Even when invoked outside the graph, PolicyGate via DbAuditTrail
    correctly rejects controlled-substance flows and persists the audit."""
    import uuid as _uuid
    from datetime import datetime, timezone

    from app.db.models import AuditRecord
    from app.db.session import get_sessionmaker
    from services.orchestrator.db_policy_gate import build_db_policy_gate

    patient = f"pg-{_uuid.uuid4().hex[:10]}"
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        gate = build_db_policy_gate(db)
        decision = await gate.evaluate(
            patient_id=patient,
            intent="medicine_ordering",
            requested_flow="order_controlled_medicine",
            now=datetime.now(timezone.utc),
        )
        await db.commit()
        assert decision.flow_action == "REJECT"
        assert "DISALLOWED_MEDICINE_ORDERING_FLOW" in decision.reason_codes

        rows = (
            await db.execute(
                select(AuditRecord).where(AuditRecord.patient_id == patient)
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].record_type == "policy_decision"
        assert rows[0].flow_action == "REJECT"
        assert "DISALLOWED_MEDICINE_ORDERING_FLOW" in rows[0].reason_codes

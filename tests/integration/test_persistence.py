"""End-to-end persistence integration tests.

Skipped automatically when DATABASE_URL is unset (so CI without DB still passes).
Each test uses a unique synthetic patient_id so reruns don't collide on data
left behind by prior runs.
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
def gateway_client():
    from services.whatsapp_gateway.main import app

    with TestClient(app) as client:
        yield client


@pytest.fixture(scope="module")
def orchestrator_client():
    # Use ``with TestClient(...)`` so the lifespan runs and resets app.state
    # — avoids picking up a stale (closed) compiled graph left behind by a
    # previous test module (e.g. test_langgraph_workflow.py).
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


@pytest.fixture()
def patient_id() -> str:
    return f"itest-{uuid.uuid4().hex[:10]}"


async def _ticket_id_for(patient: str) -> str | None:
    """Helper: the most recent ticket id for a patient (None if absent)."""
    from app.db.models import OpsTicket
    from app.db.session import get_sessionmaker

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        row = (
            await db.execute(
                select(OpsTicket.id)
                .where(OpsTicket.patient_id == patient)
                .order_by(OpsTicket.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return None if row is None else str(row)


def test_webhook_writes_inbound_to_message_log(gateway_client, patient_id):
    response = gateway_client.post(
        "/webhook",
        json={
            "message_id": f"msg-{uuid.uuid4().hex[:8]}",
            "patient_id": patient_id,
            "phone": "+10000000000",
            "text": "I took my meds",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True

    logs = gateway_client.get("/logs", params={"limit": 50}).json()
    matched = [
        entry
        for entry in logs
        if entry["direction"] == "inbound"
        and entry.get("message", {}).get("patient_id") == patient_id
    ]
    assert len(matched) == 1


def test_send_writes_outbound_to_message_log(gateway_client, patient_id):
    response = gateway_client.post(
        "/send",
        json={
            "patient_id": patient_id,
            "phone": "+10000000000",
            "body": "Reminder body",
            "use_template": True,
            "template_name": "dose_reminder_v1",
        },
    )
    assert response.status_code == 200
    assert response.json()["payload_type"] == "template"

    logs = gateway_client.get("/logs", params={"limit": 50}).json()
    matched = [
        entry
        for entry in logs
        if entry["direction"] == "outbound"
        and entry.get("message", {}).get("patient_id") == patient_id
    ]
    assert matched
    assert matched[0]["payload_type"] == "template"


async def test_route_persists_inbound_state_and_audit(orchestrator_client, patient_id):
    response = orchestrator_client.post(
        "/route",
        json={
            "message": {
                "message_id": f"msg-{uuid.uuid4().hex[:8]}",
                "patient_id": patient_id,
                "phone": "+10000000000",
                "text": "Need a refill, running low",
            }
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["intent"] == "refill_request"

    from app.db.models import AuditRecord, PatientInboundState
    from app.db.session import get_sessionmaker

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        last = (
            await db.execute(
                select(PatientInboundState.last_inbound_at).where(
                    PatientInboundState.patient_id == patient_id
                )
            )
        ).scalar_one_or_none()
        assert last is not None
        audits = (
            await db.execute(
                select(AuditRecord).where(AuditRecord.patient_id == patient_id)
            )
        ).scalars().all()
        assert audits, "expected an audit_records row"
        record_types = {a.record_type for a in audits}
        # /route always writes a workflow_summary row. In LangGraph mode, the
        # PolicyGate node also writes a policy_decision row; this test runs in
        # sync-fallback mode (LANGGRAPH_ENABLED=0 in conftest) so only the
        # workflow_summary is guaranteed.
        assert "workflow_summary" in record_types
        summary = next(a for a in audits if a.record_type == "workflow_summary")
        assert "TEMPLATE_REQUIRED_NO_INBOUND_FOUND" in summary.reason_codes or any(
            code.startswith("TEMPLATE_REQUIRED") or code.startswith("FREEFORM_ALLOWED")
            for code in summary.reason_codes
        )


def test_ops_ticket_lifecycle_against_db(orchestrator_client, patient_id):
    create = orchestrator_client.post(
        "/ops/tickets",
        json={
            "patient_id": patient_id,
            "category": "triage",
            "priority": "p1",
            "sla_minutes": 15,
            "notes": "initial",
        },
    )
    assert create.status_code == 200
    ticket = create.json()
    ticket_id = ticket["ticket_id"]
    assert ticket["status"] == "open"
    assert ticket["patient_id"] == patient_id

    listed = orchestrator_client.get("/ops/tickets", params={"status": "open"}).json()
    assert any(t["ticket_id"] == ticket_id for t in listed)

    ack = orchestrator_client.post(
        f"/ops/tickets/{ticket_id}/ack", json={"actor": "alice"}
    )
    assert ack.status_code == 200
    assert ack.json()["status"] == "acknowledged"

    resolve = orchestrator_client.post(
        f"/ops/tickets/{ticket_id}/resolve", json={"actor": "alice", "notes": "done"}
    )
    assert resolve.status_code == 200
    assert resolve.json()["status"] == "resolved"
    # Notes is now an append-only audit log: the resolve note is prepended
    # as a timestamped line, with the original "initial" preserved underneath.
    notes = resolve.json()["notes"] or ""
    assert "alice: resolved — done" in notes
    assert "initial" in notes

    missing = orchestrator_client.post(
        "/ops/tickets/999999999/ack", json={"actor": "bob"}
    )
    assert missing.status_code == 404


def test_ops_dashboard_includes_queue_counts(orchestrator_client):
    response = orchestrator_client.get("/ops/dashboard")
    assert response.status_code == 200
    body = response.json()
    assert {"adherence_rate", "refill_risk_rate", "followup_closure_rate"} <= body[
        "program_metrics"
    ].keys()
    assert {"open", "acknowledged", "resolved", "total"} <= body["queue"].keys()
    assert body["queue"]["total"] >= 0

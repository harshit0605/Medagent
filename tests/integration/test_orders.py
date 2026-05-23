"""Integration tests for the partner / refill execute layer (DB-backed).

Covers the order endpoints (create via adapter + dedupe, list, status
advance), the refill "reorder" tap → Order, and the full substitution loop
(propose → enqueue request → dispatcher render → patient approve/decline).

Skipped when DATABASE_URL is unset.
"""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import Patient, ScheduledEvent
from app.db.repositories import orders as orders_repo
from app.db.repositories import patient_inbound as patient_inbound_repo
from app.db.repositories import regimens as regimens_repo
from app.db.session import get_sessionmaker
from services.orchestrator import order_handler, refill_handler
from services.scheduler import dispatcher

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping orders integration tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


async def _seed_patient_regimen() -> tuple[int, str, int]:
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(
            full_name=f"Order Test {suffix}",
            phone=f"order-{suffix}",
            consent_sms=True,
        )
        db.add(p)
        await db.flush()
        regimen = await regimens_repo.create(
            db,
            patient_id=p.id,
            medication_name="Atorvastatin",
            dose="10 mg",
            schedule={},
        )
        await db.commit()
        return p.id, p.phone, regimen.id


# ---- order endpoints -------------------------------------------------------


async def test_create_order_endpoint_and_dedupe(orchestrator_client):
    pid, _phone, regimen_id = await _seed_patient_regimen()
    resp = orchestrator_client.post(
        f"/patients/{pid}/orders", json={"regimen_id": regimen_id}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["medication_name"] == "Atorvastatin"
    assert body["dose"] == "10 mg"
    assert body["status"] == "pending"
    assert body["partner"]  # adapter assigned a partner
    assert body["requested_via"] == "api"

    # A second in-flight order for the same regimen is rejected.
    dup = orchestrator_client.post(
        f"/patients/{pid}/orders", json={"regimen_id": regimen_id}
    )
    assert dup.status_code == 409


async def test_create_order_requires_med_or_regimen(orchestrator_client):
    pid, _phone, _r = await _seed_patient_regimen()
    resp = orchestrator_client.post(f"/patients/{pid}/orders", json={})
    assert resp.status_code == 400


async def test_list_and_status_advance(orchestrator_client):
    pid, _phone, regimen_id = await _seed_patient_regimen()
    created = orchestrator_client.post(
        f"/patients/{pid}/orders", json={"regimen_id": regimen_id}
    ).json()
    order_id = created["id"]

    listed = orchestrator_client.get(f"/patients/{pid}/orders").json()
    assert any(o["id"] == order_id for o in listed)

    advanced = orchestrator_client.post(
        f"/orders/{order_id}/status", json={"status": "shipped"}
    )
    assert advanced.status_code == 200
    assert advanced.json()["status"] == "shipped"


# ---- refill "reorder" tap → Order ------------------------------------------


async def test_refill_reorder_creates_order_idempotently():
    pid, phone, regimen_id = await _seed_patient_regimen()
    delta = await refill_handler.handle_refill_action(
        patient_phone=phone,
        new_user_text=f"[refill-action] reorder regimen_id={regimen_id}",
    )
    assert delta is not None
    assert "refill_action_reorder" in delta["audit_reasons"]

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        orders = await orders_repo.list_for_patient(db, pid)
    assert len(orders) == 1
    assert orders[0].regimen_id == regimen_id
    assert orders[0].requested_via == "refill_button"

    # Re-tapping Reorder doesn't create a second in-flight order.
    delta2 = await refill_handler.handle_refill_action(
        patient_phone=phone,
        new_user_text=f"[refill-action] reorder regimen_id={regimen_id}",
    )
    assert "refill_action_reorder_already_open" in delta2["audit_reasons"]
    async with SessionLocal() as db:
        orders = await orders_repo.list_for_patient(db, pid)
    assert len(orders) == 1


# ---- substitution loop -----------------------------------------------------


async def _make_order(pid: int, regimen_id: int) -> int:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        order = await orders_repo.create(
            db,
            patient_id=pid,
            regimen_id=regimen_id,
            medication_name="Atorvastatin",
            dose="10 mg",
            partner="acme_rx",
        )
        await db.commit()
        return order.id


async def test_propose_substitution_enqueues_request(orchestrator_client):
    pid, phone, regimen_id = await _seed_patient_regimen()
    order_id = await _make_order(pid, regimen_id)

    resp = orchestrator_client.post(
        f"/orders/{order_id}/propose-substitution",
        json={"medication": "Atorvastatin (Brand B)", "note": "out of stock"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["substitution_status"] == "proposed"
    assert resp.json()["substitution_medication"] == "Atorvastatin (Brand B)"

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        stmt = (
            select(ScheduledEvent)
            .where(ScheduledEvent.patient_id == phone)
            .where(
                ScheduledEvent.event_type == "order_substitution_request"
            )
        )
        events = list((await db.execute(stmt)).scalars().all())
    assert len(events) == 1
    assert events[0].payload["order_id"] == order_id


async def test_order_substitution_approve_and_decline():
    pid, phone, regimen_id = await _seed_patient_regimen()

    # Approve path.
    order_id = await _make_order(pid, regimen_id)
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await orders_repo.propose_substitution(
            db, order_id, medication="Atorvastatin (Brand B)"
        )
        await db.commit()

    delta = await order_handler.handle_order_action(
        patient_phone=phone,
        new_user_text=f"[order-action] sub_approve order_id={order_id}",
    )
    assert "order_action_substitution_approved" in delta["audit_reasons"]
    async with SessionLocal() as db:
        order = await orders_repo.get(db, order_id)
    assert order.substitution_status == "approved"

    # Decline path on a fresh order.
    order_id2 = await _make_order(pid, regimen_id)
    async with SessionLocal() as db:
        await orders_repo.propose_substitution(
            db, order_id2, medication="Generic X"
        )
        await db.commit()
    delta2 = await order_handler.handle_order_action(
        patient_phone=phone,
        new_user_text=f"[order-action] sub_decline order_id={order_id2}",
    )
    assert "order_action_substitution_declined" in delta2["audit_reasons"]
    async with SessionLocal() as db:
        order2 = await orders_repo.get(db, order_id2)
    assert order2.substitution_status == "declined"


async def test_order_action_no_pending_substitution():
    pid, phone, regimen_id = await _seed_patient_regimen()
    order_id = await _make_order(pid, regimen_id)  # no substitution proposed
    delta = await order_handler.handle_order_action(
        patient_phone=phone,
        new_user_text=f"[order-action] sub_approve order_id={order_id}",
    )
    assert "order_action_no_pending_substitution" in delta["audit_reasons"]


# ---- dispatcher render -----------------------------------------------------


async def test_dispatcher_builds_substitution_request_template():
    pid, phone, regimen_id = await _seed_patient_regimen()
    order_id = await _make_order(pid, regimen_id)
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await orders_repo.propose_substitution(
            db, order_id, medication="Atorvastatin (Brand B)"
        )
        await db.commit()

    event = SimpleNamespace(
        id=7001,
        event_type="order_substitution_request",
        patient_id=phone,
        payload={"order_id": order_id},
    )
    # No prior inbound → out-of-CSW → template send.
    async with SessionLocal() as db:
        msg = await dispatcher._build_substitution_request(db, event)
    assert msg["use_template"] is True
    assert msg["template_name"] == "substitution_approval_v1"
    assert msg["template_params"]["1_original"] == "Atorvastatin"
    assert msg["template_params"]["2_substitute"] == "Atorvastatin (Brand B)"


async def test_dispatcher_substitution_freeform_in_csw():
    pid, phone, regimen_id = await _seed_patient_regimen()
    order_id = await _make_order(pid, regimen_id)
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await orders_repo.propose_substitution(
            db, order_id, medication="Generic X"
        )
        from datetime import datetime, timezone

        await patient_inbound_repo.set_last_inbound(
            db, phone, datetime.now(timezone.utc)
        )
        await db.commit()

    event = SimpleNamespace(
        id=7002,
        event_type="order_substitution_request",
        patient_id=phone,
        payload={"order_id": order_id},
    )
    async with SessionLocal() as db:
        msg = await dispatcher._build_substitution_request(db, event)
    assert msg["use_template"] is False
    actions = {b["action"] for b in msg["buttons"]}
    assert actions == {"order_sub_approve", "order_sub_decline"}


# ---- /route end-to-end -----------------------------------------------------


async def test_route_order_action_end_to_end(orchestrator_client):
    pid, phone, regimen_id = await _seed_patient_regimen()
    order_id = await _make_order(pid, regimen_id)
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await orders_repo.propose_substitution(
            db, order_id, medication="Brand B"
        )
        await db.commit()

    resp = orchestrator_client.post(
        "/route",
        json={
            "message": {
                "patient_id": phone,
                "text": f"[order-action] sub_approve order_id={order_id}",
                "message_id": f"wamid-{uuid.uuid4().hex[:8]}",
            },
        },
    )
    assert resp.status_code == 200
    async with SessionLocal() as db:
        order = await orders_repo.get(db, order_id)
    assert order.substitution_status == "approved"


# ---- delivery receipts (MVP #7) --------------------------------------------


async def test_status_delivered_enqueues_receipt_once(orchestrator_client):
    pid, phone, regimen_id = await _seed_patient_regimen()
    order_id = await _make_order(pid, regimen_id)

    resp = orchestrator_client.post(
        f"/orders/{order_id}/status", json={"status": "delivered"}
    )
    assert resp.status_code == 200

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        stmt = (
            select(ScheduledEvent)
            .where(ScheduledEvent.patient_id == phone)
            .where(ScheduledEvent.event_type == "order_receipt")
        )
        events = list((await db.execute(stmt)).scalars().all())
    assert len(events) == 1
    assert events[0].payload["order_id"] == order_id

    # Re-setting delivered must NOT enqueue a second receipt.
    orchestrator_client.post(
        f"/orders/{order_id}/status", json={"status": "delivered"}
    )
    async with SessionLocal() as db:
        events2 = list(
            (await db.execute(stmt)).scalars().all()
        )
    assert len(events2) == 1


async def test_dispatcher_builds_order_receipt_template():
    pid, phone, regimen_id = await _seed_patient_regimen()
    order_id = await _make_order(pid, regimen_id)

    event = SimpleNamespace(
        id=8001,
        event_type="order_receipt",
        patient_id=phone,
        payload={"order_id": order_id},
    )
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        msg = await dispatcher._build_order_receipt(db, event)
    assert msg["use_template"] is True
    assert msg["template_name"] == "order_receipt_v1"
    assert msg["template_params"]["2_ref"] == f"ORD-{order_id}"
    assert "Atorvastatin" in msg["template_params"]["1_med"]

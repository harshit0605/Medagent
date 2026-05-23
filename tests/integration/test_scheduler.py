"""End-to-end scheduler tests against Supabase.

Skipped without DATABASE_URL. Disable the background polling loop so the
test drives every cycle deterministically via /scheduler/tick. The gateway
is a stand-in TestClient — the dispatcher is monkeypatched to call it
instead of opening a real HTTP socket.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient


pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping scheduler integration tests",
)


# Scheduler tests call ``/scheduler/tick`` which fetches every
# DUE event globally. With pollution from broadcast / clinical-
# alert / care-gap tests, the tick processes hundreds of
# unrelated events before reaching this test's seeded one —
# slow, and the per-test patient_id assertions get diluted by
# noise from accumulated rows.
#
# File-scoped autouse cleanup wipes pending events older than
# 1 minute before each test runs. The test seeds a fresh
# event after the cleanup, so the tick sees only that event.
# Sync (def) so it works with this file's mix of sync test
# functions.
@pytest.fixture(autouse=True)
def _isolate_scheduled_events():
    """Drop stale pending events before each scheduler test
    so the tick processes only what THIS test seeded."""
    import asyncio as _asyncio

    from sqlalchemy import text as sql_text

    async def _wipe() -> None:
        from app.db.session import get_engine

        engine = get_engine()
        async with engine.begin() as conn:
            # Wipe ALL pending events before each test so the
            # tick processes only what THIS test seeds. Other
            # tests in the same process can leak pending
            # events <1 min old that the broader cleanup
            # doesn't catch; this file's tests aren't
            # interested in cross-test state. Failed and
            # dispatched events stay (post-tick state, tests
            # inspect them in some assertions).
            await conn.execute(
                sql_text(
                    "DELETE FROM scheduled_events WHERE "
                    "status = 'pending'"
                )
            )

    _asyncio.run(_wipe())
    yield


@pytest.fixture(scope="module")
def gateway_client():
    from services.whatsapp_gateway.main import app

    with TestClient(app) as client:
        yield client


@pytest.fixture(scope="module")
def scheduler_client():
    """Bring up the scheduler with the polling loop OFF so /scheduler/tick is the
    only path that dispatches."""
    prior = os.environ.get("SCHEDULER_ENABLED")
    os.environ["SCHEDULER_ENABLED"] = "0"

    import importlib
    import services.scheduler.main as scheduler_main

    scheduler_main = importlib.reload(scheduler_main)

    with TestClient(scheduler_main.app) as client:
        yield client

    if prior is None:
        os.environ.pop("SCHEDULER_ENABLED", None)
    else:
        os.environ["SCHEDULER_ENABLED"] = prior


@pytest.fixture()
def patient_id() -> str:
    return f"sched-{uuid.uuid4().hex[:10]}"


@pytest.fixture()
def patch_dispatch_to_gateway(monkeypatch):
    """Redirect the dispatcher's outbound POST to the in-process gateway app.

    Earlier this bridged to a *sync* gateway TestClient, but the dispatcher
    runs inside the scheduler app's own event loop (driven by the scheduler
    TestClient's BlockingPortal). Calling a sync TestClient from that loop
    thread tripped anyio's "cannot be called from the event loop thread"
    guard. The fix: route through the gateway's ASGI app with a *real,
    async* ``httpx.AsyncClient`` backed by ``ASGITransport`` — it runs in
    the same loop, no thread hop. We capture the genuine ``AsyncClient``
    class before patching so the factory doesn't recurse into itself.
    """
    import httpx

    from services.scheduler import dispatcher as dispatcher_module
    from services.whatsapp_gateway.main import app as gateway_app

    real_async_client = httpx.AsyncClient  # capture before monkeypatch
    transport = httpx.ASGITransport(app=gateway_app)

    def factory(*_a, **_k):  # noqa: ARG001 — dispatcher passes timeout=, ignored
        return real_async_client(
            transport=transport, base_url="http://gateway.test"
        )

    monkeypatch.setattr(dispatcher_module.httpx, "AsyncClient", factory)


def _list_pending_for(scheduler_client, patient_id: str) -> list[dict]:
    return scheduler_client.get(
        "/scheduled-events",
        params={"patient_id": patient_id, "status": "pending"},
    ).json()


def test_emit_dose_due_inserts_pending_row(scheduler_client, patient_id):
    response = scheduler_client.post(
        "/emit-dose-due",
        json={
            "patient_id": patient_id,
            "regimen_id": "r-1",
            "medication_name": "metformin",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["event_type"] == "dose_due"
    assert body["status"] == "pending"
    assert body["patient_id"] == patient_id

    pending = _list_pending_for(scheduler_client, patient_id)
    assert len(pending) == 1


def _seed_patient_and_regimen(phone: str) -> int:
    """Seed a patient (phone=wa_id) + an active regimen; return
    regimen_id. The dispatcher's refill branch looks the regimen up to
    validate it + build the Refilled button, so a real row must exist."""
    import asyncio as _asyncio

    from app.db.models import Patient
    from app.db.repositories import regimens as regimens_repo
    from app.db.session import get_sessionmaker

    async def _seed() -> int:
        SessionLocal = get_sessionmaker()
        async with SessionLocal() as db:
            p = Patient(
                full_name="Refill Dispatch Test",
                phone=phone,
                consent_sms=True,
            )
            db.add(p)
            await db.flush()
            r = await regimens_repo.create(
                db,
                patient_id=p.id,
                medication_name="atorvastatin",
                dose="10 mg",
                schedule={
                    "type": "times_of_day",
                    "times": ["20:00"],
                    "timezone": "Asia/Kolkata",
                    "frequency": "daily",
                },
            )
            await db.commit()
            return r.id

    return _asyncio.run(_seed())


def test_tick_dispatches_due_events_and_marks_dispatched(
    scheduler_client, patient_id, patch_dispatch_to_gateway, gateway_client
):
    regimen_id = _seed_patient_and_regimen(patient_id)
    response = scheduler_client.post(
        "/emit-refill-due",
        json={
            "patient_id": patient_id,
            "regimen_id": regimen_id,
            "medication_name": "atorvastatin",
            "dose": "10 mg",
            "days_left": 3,
        },
    )
    assert response.status_code == 200

    # Drive one poll cycle manually.
    tick = scheduler_client.post("/scheduler/tick").json()
    assert tick["dispatched"] >= 1

    # Row should now be dispatched.
    pending_after = _list_pending_for(scheduler_client, patient_id)
    assert pending_after == []

    # Gateway should have logged the outbound template.
    logs = gateway_client.get("/logs", params={"limit": 50}).json()
    matched = [
        e for e in logs
        if e["direction"] == "outbound"
        and e.get("message", {}).get("patient_id") == patient_id
        and e.get("message", {}).get("template_name") == "refill_due_v1"
    ]
    assert matched, "expected refill_due_v1 outbound log row for this patient"


def test_future_event_is_not_dispatched_until_due(
    scheduler_client, patient_id, patch_dispatch_to_gateway
):
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    response = scheduler_client.post(
        "/emit-dose-due",
        json={
            "patient_id": patient_id,
            "regimen_id": "r-future",
            "medication_name": "lisinopril",
            "scheduled_for": future,
        },
    )
    assert response.status_code == 200

    tick = scheduler_client.post("/scheduler/tick").json()
    # No event due yet.
    assert tick["dispatched"] == 0

    pending = _list_pending_for(scheduler_client, patient_id)
    assert len(pending) >= 1


def test_tick_marks_failed_when_dispatch_errors(
    scheduler_client, patient_id, monkeypatch
):
    # Force every async dispatch to fail.
    import httpx

    from services.scheduler import dispatcher as dispatcher_module

    class _BoomClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

        async def post(self, *_a, **_k):
            raise httpx.ConnectError("nope")

    monkeypatch.setattr(dispatcher_module.httpx, "AsyncClient", lambda **_k: _BoomClient())

    scheduler_client.post(
        "/emit-triage-alert",
        json={
            "patient_id": patient_id,
            "cohort": "diabetes",
            "severity": "high",
            "reason": "hypo episode reported",
        },
    )

    tick = scheduler_client.post("/scheduler/tick").json()
    assert tick["failed"] >= 1

    failed = scheduler_client.get(
        "/scheduled-events",
        params={"patient_id": patient_id, "status": "failed"},
    ).json()
    assert any(e["event_type"] == "triage_alert" for e in failed)
    assert any(e["error"].startswith("http_error:") for e in failed)

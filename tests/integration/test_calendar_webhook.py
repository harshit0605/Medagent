"""Integration tests for the Google Calendar webhook push endpoint.

The actual incremental sync is monkeypatched (no live Google API); we assert
the endpoint's routing: handshake ack, missing-token no-op, and change →
push-sync for the doctor identified by the channel token.

Skipped when DATABASE_URL is unset (endpoint depends on a DB session).
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping calendar webhook tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


def test_handshake_ping_is_acked(orchestrator_client):
    resp = orchestrator_client.post(
        "/webhooks/google-calendar",
        headers={
            "X-Goog-Resource-State": "sync",
            "X-Goog-Channel-ID": "ch-1",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["handshake"] is True


def test_change_without_doctor_token_is_noop(orchestrator_client):
    resp = orchestrator_client.post(
        "/webhooks/google-calendar",
        headers={
            "X-Goog-Resource-State": "exists",
            "X-Goog-Channel-ID": "ch-1",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["synced"] is False
    assert body["reason"] == "bad_token"


def test_change_triggers_push_sync(orchestrator_client, monkeypatch):
    from services.scheduler import calendar_sync_sweep

    async def fake_reconcile(_db, *, doctor_id):
        assert doctor_id == 7
        return {"changes_received": 2}

    monkeypatch.setattr(
        calendar_sync_sweep, "reconcile_doctor", fake_reconcile
    )
    resp = orchestrator_client.post(
        "/webhooks/google-calendar",
        headers={
            "X-Goog-Resource-State": "exists",
            "X-Goog-Channel-Token": "7",
            "X-Goog-Channel-ID": "ch-1",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["synced"] is True
    assert body["doctor_id"] == 7
    assert body["changes"] == 2


def test_sync_error_still_acks_200(orchestrator_client, monkeypatch):
    from services.scheduler import calendar_sync_sweep

    async def boom(_db, *, doctor_id):
        raise RuntimeError("google down")

    monkeypatch.setattr(calendar_sync_sweep, "reconcile_doctor", boom)
    resp = orchestrator_client.post(
        "/webhooks/google-calendar",
        headers={
            "X-Goog-Resource-State": "exists",
            "X-Goog-Channel-Token": "9",
        },
    )
    # Always 200 so Google doesn't enter aggressive retry.
    assert resp.status_code == 200
    assert resp.json()["synced"] is False
    assert resp.json()["reason"] == "sync_error"

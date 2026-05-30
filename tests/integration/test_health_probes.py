"""Liveness / readiness probe tests for the orchestrator.

Liveness must be a trivial 200 that never touches dependencies; readiness
pings the DB and reports per-check status. Both must be auth-exempt so a
load balancer can hit them without the shared secret.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping readiness probe (needs DB)",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


def test_liveness_is_trivial_ok(orchestrator_client):
    r = orchestrator_client.get("/health/live")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_legacy_health_still_ok(orchestrator_client):
    # The old /health stays the liveness probe for backward compat.
    r = orchestrator_client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_readiness_reports_db_check(orchestrator_client):
    r = orchestrator_client.get("/health/ready")
    # DB is configured in the integration env, so readiness should be 200.
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ready"
    assert body["checks"]["db"]["ok"] is True


def test_readiness_is_auth_exempt(orchestrator_client, monkeypatch):
    """Even with a key configured (auth enforced), readiness must answer
    without the X-API-Key header — load balancers don't send it."""
    from services.orchestrator import main as orch_main

    monkeypatch.setattr(orch_main, "_ORCH_API_KEY", "some-secret-key")
    r = orchestrator_client.get("/health/ready")
    assert r.status_code in (200, 503)  # answered, not 401
    r2 = orchestrator_client.get("/health/live")
    assert r2.status_code == 200

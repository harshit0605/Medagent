"""Orchestrator API-key middleware + calendar-webhook token hardening.

The middleware reads the module-global key at request time, so we monkeypatch
``main._ORCH_API_KEY`` to exercise the enabled path without touching the
process env. DATABASE_URL required (the protected probe endpoint hits the DB).
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from services.orchestrator import main as orch_main

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping API auth tests",
)


@pytest.fixture(scope="module")
def client():
    with TestClient(orch_main.app) as c:
        yield c


def test_protected_endpoint_rejects_missing_key(client, monkeypatch):
    monkeypatch.setattr(orch_main, "_ORCH_API_KEY", "test-secret")
    resp = client.get("/ops/dashboard")
    assert resp.status_code == 401


def test_protected_endpoint_accepts_valid_key(client, monkeypatch):
    monkeypatch.setattr(orch_main, "_ORCH_API_KEY", "test-secret")
    resp = client.get("/ops/dashboard", headers={"x-api-key": "test-secret"})
    assert resp.status_code != 401


def test_protected_endpoint_accepts_bearer(client, monkeypatch):
    monkeypatch.setattr(orch_main, "_ORCH_API_KEY", "test-secret")
    resp = client.get(
        "/ops/dashboard", headers={"authorization": "Bearer test-secret"}
    )
    assert resp.status_code != 401


def test_wrong_key_rejected(client, monkeypatch):
    monkeypatch.setattr(orch_main, "_ORCH_API_KEY", "test-secret")
    resp = client.get("/ops/dashboard", headers={"x-api-key": "nope"})
    assert resp.status_code == 401


def test_health_is_exempt(client, monkeypatch):
    monkeypatch.setattr(orch_main, "_ORCH_API_KEY", "test-secret")
    resp = client.get("/health")
    assert resp.status_code == 200


def test_auth_disabled_when_key_unset(client, monkeypatch):
    monkeypatch.setattr(orch_main, "_ORCH_API_KEY", "")
    resp = client.get("/ops/dashboard")
    assert resp.status_code != 401


def test_openapi_requires_key_when_enabled(client, monkeypatch):
    # The schema enumerates every PHI endpoint — it must NOT be exempt.
    monkeypatch.setattr(orch_main, "_ORCH_API_KEY", "test-secret")
    assert client.get("/openapi.json").status_code == 401
    assert client.get("/docs").status_code == 401


def test_health_exempt_not_overmatched(client, monkeypatch):
    # /health is exact-exempt; a lookalike prefix must still require the key
    # (previously a broad startswith("/health") would have let it through).
    monkeypatch.setattr(orch_main, "_ORCH_API_KEY", "test-secret")
    assert client.get("/health").status_code == 200
    assert client.get("/healthz").status_code == 401


# ---- fail-closed startup guard ---------------------------------------------


def test_check_auth_config_fails_closed_when_unset(monkeypatch):
    monkeypatch.setattr(orch_main, "_ORCH_API_KEY", "")
    monkeypatch.setattr(orch_main, "_ALLOW_UNAUTHENTICATED", False)
    with pytest.raises(RuntimeError):
        orch_main._check_auth_config()


def test_check_auth_config_allows_explicit_optin(monkeypatch):
    monkeypatch.setattr(orch_main, "_ORCH_API_KEY", "")
    monkeypatch.setattr(orch_main, "_ALLOW_UNAUTHENTICATED", True)
    orch_main._check_auth_config()  # must not raise


def test_check_auth_config_allows_when_key_set(monkeypatch):
    monkeypatch.setattr(orch_main, "_ORCH_API_KEY", "a-key")
    monkeypatch.setattr(orch_main, "_ALLOW_UNAUTHENTICATED", False)
    orch_main._check_auth_config()  # must not raise


# ---- calendar webhook token hardening --------------------------------------


def _gcal_headers(token: str, state: str = "exists") -> dict[str, str]:
    return {
        "X-Goog-Resource-State": state,
        "X-Goog-Channel-Token": token,
        "X-Goog-Channel-ID": "chan-test",
    }


def test_webhook_exempt_from_api_key_but_validates_token(client, monkeypatch):
    # API key enforced globally, but the webhook is exempt (it has its own
    # secret). With GCAL_WEBHOOK_TOKEN set, a bare doctor-id token is rejected.
    monkeypatch.setattr(orch_main, "_ORCH_API_KEY", "test-secret")
    monkeypatch.setenv("GCAL_WEBHOOK_TOKEN", "wh-secret")

    # No API key header at all — still reaches the handler (exempt).
    resp = client.post("/webhooks/google-calendar", headers=_gcal_headers("1"))
    assert resp.status_code == 200
    assert resp.json()["reason"] == "bad_token"  # secret required now


def test_webhook_rejects_wrong_secret(client, monkeypatch):
    monkeypatch.setenv("GCAL_WEBHOOK_TOKEN", "wh-secret")
    resp = client.post(
        "/webhooks/google-calendar", headers=_gcal_headers("1:wrong")
    )
    assert resp.status_code == 200
    assert resp.json()["reason"] == "bad_token"


def test_webhook_accepts_valid_secret(client, monkeypatch):
    monkeypatch.setenv("GCAL_WEBHOOK_TOKEN", "wh-secret")
    resp = client.post(
        "/webhooks/google-calendar", headers=_gcal_headers("1:wh-secret")
    )
    assert resp.status_code == 200
    # Passed token validation → it attempted a sync (synced True/False), not
    # rejected as a bad token.
    assert resp.json().get("reason") != "bad_token"


def test_webhook_handshake_acked(client, monkeypatch):
    monkeypatch.setenv("GCAL_WEBHOOK_TOKEN", "wh-secret")
    resp = client.post(
        "/webhooks/google-calendar",
        headers=_gcal_headers("anything", state="sync"),
    )
    assert resp.status_code == 200
    assert resp.json().get("handshake") is True

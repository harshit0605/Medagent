"""Integration tests for the WhatsApp status receipt endpoints.

Hits Supabase. Skipped without DATABASE_URL.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient


pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping integration tests",
)


@pytest.fixture(scope="module")
def gateway_client():
    from services.whatsapp_gateway.main import app

    with TestClient(app) as client:
        yield client


def _wamid() -> str:
    return f"wamid.itest-{uuid.uuid4().hex[:12]}"


def _post_status(client, **overrides):
    body = {
        "wamid": overrides.pop("wamid", _wamid()),
        "status": "sent",
        "recipient_id": "16315551234",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "raw": {},
    }
    body.update(overrides)
    response = client.post("/internal/whatsapp/status", json=body)
    response.raise_for_status()
    return response.json()


def test_upsert_creates_a_row(gateway_client):
    body = _post_status(gateway_client)
    assert body["status"] == "sent"
    assert body["recipient_id"] == "16315551234"


def test_status_advances_through_lifecycle(gateway_client):
    wamid = _wamid()
    base = datetime.now(timezone.utc) - timedelta(seconds=30)

    sent = _post_status(
        gateway_client,
        wamid=wamid,
        status="sent",
        timestamp=base.isoformat(),
    )
    assert sent["status"] == "sent"

    delivered = _post_status(
        gateway_client,
        wamid=wamid,
        status="delivered",
        timestamp=(base + timedelta(seconds=10)).isoformat(),
    )
    assert delivered["status"] == "delivered"

    read = _post_status(
        gateway_client,
        wamid=wamid,
        status="read",
        timestamp=(base + timedelta(seconds=20)).isoformat(),
    )
    assert read["status"] == "read"

    # A late, lower-rank `delivered` should NOT regress the persisted `read`.
    late_delivered = _post_status(
        gateway_client,
        wamid=wamid,
        status="delivered",
        timestamp=(base + timedelta(seconds=25)).isoformat(),
    )
    assert late_delivered["status"] == "read", "rank guard should keep `read`"


def test_failed_status_carries_error_details(gateway_client):
    body = _post_status(
        gateway_client,
        status="failed",
        error_code=131000,
        error_title="Generic error",
    )
    assert body["status"] == "failed"
    assert body["error_code"] == 131000
    assert body["error_title"] == "Generic error"


def test_list_filters_by_recipient(gateway_client):
    recipient_a = f"itest-a-{uuid.uuid4().hex[:8]}"
    recipient_b = f"itest-b-{uuid.uuid4().hex[:8]}"
    _post_status(gateway_client, recipient_id=recipient_a)
    _post_status(gateway_client, recipient_id=recipient_b)

    a_only = gateway_client.get(
        "/whatsapp/statuses", params={"recipient_id": recipient_a, "limit": 50}
    ).json()
    assert all(row["recipient_id"] == recipient_a for row in a_only)
    assert any(row["recipient_id"] == recipient_a for row in a_only)

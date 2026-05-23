"""Integration tests for the doctor-inbox flow — /route persists a
classification row, /ops/inbox returns it.

LLM is disabled in conftest, so the classifier hits the deterministic
fallback path (category=unknown, summary=raw text). That's the only
contract we're testing here — the LLM-powered path is exercised in
unit tests with a mocked client.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping inbox integration tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


@pytest.fixture()
def patient_phone() -> str:
    return f"inbox-test-{uuid.uuid4().hex[:10]}"


def test_route_persists_inbox_row(orchestrator_client, patient_phone):
    """/route on a freeform inbound must create one inbound_classifications
    row, joinable from the inbox endpoint by patient_phone."""
    text = "Hi, my blood sugar reading was 280 this morning"
    response = orchestrator_client.post(
        "/route",
        json={
            "message": {
                "message_id": f"msg-{uuid.uuid4().hex[:8]}",
                "patient_id": patient_phone,
                "phone": patient_phone,
                "text": text,
            }
        },
    )
    assert response.status_code == 200

    inbox = orchestrator_client.get(
        "/ops/inbox", params={"patient_phone": patient_phone}
    ).json()
    assert len(inbox) == 1
    row = inbox[0]
    assert row["patient_phone"] == patient_phone
    assert row["inbound_text"] == text
    # LLM is disabled in tests → deterministic fallback path.
    assert row["category"] == "unknown"
    # Summary falls back to the raw inbound when LLM is off.
    assert "280" in (row["summary"] or "")
    # handler_used is one of the known paths the inferer outputs.
    assert row["handler_used"] is not None


def test_action_tap_persists_with_action_tap_category(
    orchestrator_client, patient_phone
):
    """A tap-routed inbound (marker prefix) skips the LLM and lands as
    category=action_tap so the inbox can group it under the Tap chip."""
    response = orchestrator_client.post(
        "/route",
        json={
            "message": {
                "message_id": f"msg-{uuid.uuid4().hex[:8]}",
                "patient_id": patient_phone,
                "phone": patient_phone,
                "text": "[dose-action] taken adherence_event_id=999999",
            }
        },
    )
    assert response.status_code == 200

    inbox = orchestrator_client.get(
        "/ops/inbox", params={"patient_phone": patient_phone}
    ).json()
    assert len(inbox) == 1
    assert inbox[0]["category"] == "action_tap"


def test_inbox_filters_compose_with_and(orchestrator_client, patient_phone):
    """Two messages from the same patient → category filter narrows to
    the matching one."""
    orchestrator_client.post(
        "/route",
        json={
            "message": {
                "message_id": f"msg-{uuid.uuid4().hex[:8]}",
                "patient_id": patient_phone,
                "phone": patient_phone,
                "text": "Plain freeform question",
            }
        },
    )
    orchestrator_client.post(
        "/route",
        json={
            "message": {
                "message_id": f"msg-{uuid.uuid4().hex[:8]}",
                "patient_id": patient_phone,
                "phone": patient_phone,
                "text": "[lab-action] booked lab_followup_id=999999",
            }
        },
    )

    all_rows = orchestrator_client.get(
        "/ops/inbox", params={"patient_phone": patient_phone}
    ).json()
    assert len(all_rows) >= 2

    # Filter to action_tap only.
    only_taps = orchestrator_client.get(
        "/ops/inbox",
        params={"patient_phone": patient_phone, "category": "action_tap"},
    ).json()
    assert len(only_taps) >= 1
    assert all(r["category"] == "action_tap" for r in only_taps)
    # The freeform one is excluded.
    assert all(r["inbound_text"] != "Plain freeform question" for r in only_taps)


def test_voice_input_persists_with_voice_kind(
    orchestrator_client, patient_phone
):
    """Webhook signals ``input_kind=voice`` for transcribed audio. The
    inbox row must reflect that so the UI can show the voice badge."""
    response = orchestrator_client.post(
        "/route",
        json={
            "message": {
                "message_id": f"msg-{uuid.uuid4().hex[:8]}",
                "patient_id": patient_phone,
                "phone": patient_phone,
                "text": "I've been having trouble sleeping for two weeks",
                "input_kind": "voice",
            }
        },
    )
    assert response.status_code == 200

    inbox = orchestrator_client.get(
        "/ops/inbox", params={"patient_phone": patient_phone}
    ).json()
    assert len(inbox) == 1
    assert inbox[0]["input_kind"] == "voice"


def test_input_kind_filter_narrows_results(
    orchestrator_client, patient_phone
):
    """Two messages with different input kinds — filter narrows to one."""
    orchestrator_client.post(
        "/route",
        json={
            "message": {
                "message_id": f"msg-{uuid.uuid4().hex[:8]}",
                "patient_id": patient_phone,
                "phone": patient_phone,
                "text": "Typed plain text",
                "input_kind": "text",
            }
        },
    )
    orchestrator_client.post(
        "/route",
        json={
            "message": {
                "message_id": f"msg-{uuid.uuid4().hex[:8]}",
                "patient_id": patient_phone,
                "phone": patient_phone,
                "text": "Transcribed voice content",
                "input_kind": "voice",
            }
        },
    )

    only_voice = orchestrator_client.get(
        "/ops/inbox",
        params={"patient_phone": patient_phone, "input_kind": "voice"},
    ).json()
    assert len(only_voice) == 1
    assert only_voice[0]["inbound_text"] == "Transcribed voice content"


def test_input_kind_inferred_from_action_tap_marker(
    orchestrator_client, patient_phone
):
    """Backwards compat: an action-tap inbound without explicit
    ``input_kind`` should still land as kind=button so old webhook
    builds + integration smoke tests get sensible badges."""
    orchestrator_client.post(
        "/route",
        json={
            "message": {
                "message_id": f"msg-{uuid.uuid4().hex[:8]}",
                "patient_id": patient_phone,
                "phone": patient_phone,
                "text": "[dose-action] taken adherence_event_id=999999",
                # Note: no input_kind — orchestrator should sniff the marker.
            }
        },
    )

    inbox = orchestrator_client.get(
        "/ops/inbox", params={"patient_phone": patient_phone}
    ).json()
    assert any(r["input_kind"] == "button" for r in inbox)


def test_inbox_category_counts_endpoint(orchestrator_client, patient_phone):
    """/ops/inbox/category-counts returns a {category → count} dict
    with one row per known category (zero-filled)."""
    # Seed at least one row.
    orchestrator_client.post(
        "/route",
        json={
            "message": {
                "message_id": f"msg-{uuid.uuid4().hex[:8]}",
                "patient_id": patient_phone,
                "phone": patient_phone,
                "text": "category-counts seed",
            }
        },
    )

    counts = orchestrator_client.get(
        "/ops/inbox/category-counts", params={"days": 7}
    ).json()
    # All known categories present (zero-filled where applicable).
    expected = {
        "clinical_question",
        "administrative",
        "billing",
        "scheduling",
        "faq",
        "social",
        "unsafe",
        "action_tap",
        "unknown",
    }
    assert expected <= set(counts.keys())
    # And the seed message bumped at least one bucket above zero.
    assert sum(counts.values()) >= 1

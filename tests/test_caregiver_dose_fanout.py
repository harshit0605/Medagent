"""Unit tests for the caregiver dose-reminder fan-out builder.

Verifies the global flag + per-caregiver opt-in gate without spinning up a
real DB (uses minimal SimpleNamespace stand-ins for the SQLAlchemy rows the
builder reads from its session-bound repos)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from services.scheduler import dispatcher


def _stub_event():
    return SimpleNamespace(
        id=42,
        event_type="dose_due",
        patient_id="+919900000000",
        payload={
            "adherence_event_id": 7,
            "medication_name": "Metformin",
            "dose": "500mg",
        },
    )


@pytest.mark.asyncio
async def test_fanout_returns_empty_when_global_flag_off():
    """Even with caregivers + adherence + a patient present, the flag is the
    kill switch."""
    with patch.object(dispatcher, "_CAREGIVER_DOSE_FANOUT_ENABLED", False):
        out = await dispatcher._dose_caregiver_fanout_messages(
            AsyncMock(), _stub_event()
        )
    assert out == []


@pytest.mark.asyncio
async def test_fanout_returns_empty_when_no_caregivers_opted_in():
    """Flag on, but list_active_dose_recipients returns []."""
    db = AsyncMock()
    with patch.object(dispatcher, "_CAREGIVER_DOSE_FANOUT_ENABLED", True), \
         patch(
            "app.db.repositories.adherence_events.get",
            new=AsyncMock(return_value=SimpleNamespace(patient_id=5)),
         ), \
         patch(
            "app.db.repositories.caregivers.list_active_dose_recipients",
            new=AsyncMock(return_value=[]),
         ):
        out = await dispatcher._dose_caregiver_fanout_messages(db, _stub_event())
    assert out == []


@pytest.mark.asyncio
async def test_fanout_returns_empty_when_adherence_missing():
    """A stale dose event for a deleted adherence row should produce no
    fanouts (the primary builder also raises ReminderNotApplicable)."""
    db = AsyncMock()
    with patch.object(dispatcher, "_CAREGIVER_DOSE_FANOUT_ENABLED", True), \
         patch(
            "app.db.repositories.adherence_events.get",
            new=AsyncMock(return_value=None),
         ):
        out = await dispatcher._dose_caregiver_fanout_messages(db, _stub_event())
    assert out == []


@pytest.mark.asyncio
async def test_fanout_builds_template_messages_per_caregiver():
    db = AsyncMock()
    fake_caregivers = [
        SimpleNamespace(phone="+919900011111", full_name="Ananya Shah"),
        SimpleNamespace(phone="+919900022222", full_name="Pratik Mehra"),
    ]
    with patch.object(dispatcher, "_CAREGIVER_DOSE_FANOUT_ENABLED", True), \
         patch(
            "app.db.repositories.adherence_events.get",
            new=AsyncMock(return_value=SimpleNamespace(patient_id=5)),
         ), \
         patch(
            "app.db.repositories.caregivers.list_active_dose_recipients",
            new=AsyncMock(return_value=fake_caregivers),
         ), \
         patch.object(
            dispatcher,
            "_patient_first_name",
            new=AsyncMock(return_value="Asha"),
         ):
        out = await dispatcher._dose_caregiver_fanout_messages(db, _stub_event())

    assert len(out) == 2
    phones = {m["patient_id"] for m in out}
    assert phones == {"+919900011111", "+919900022222"}
    for msg in out:
        assert msg["use_template"] is True
        assert msg["template_name"] == "caregiver_dose_reminder_v1"
        assert msg["template_params"]["1_name"] == "Asha"
        assert msg["template_params"]["2_med"] == "Metformin (500mg)"
        # Each caregiver message identifies the patient by first name, not phone.
        assert "Asha" in msg["body"]
        assert "+919900000000" not in msg["body"]

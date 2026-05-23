"""Unit tests for the dispatcher's opt-out consent gate.

The gate runs before any message-building code, so opted-out patients
get a ``not_applicable:`` skip BEFORE the dispatcher tries to resolve
templates / appointments / regimens. That's important because:

    - The skip is treated as success by the scheduler (no retry storm).
    - We never POST to the gateway, never increment delivery metrics
      with would-have-been-sent rows.
    - Opted-out + missing data downstream both surface as the same
      "skip" outcome — the patient state, not the data state, drives
      the suppression.

DB calls are mocked at the patients_repo boundary so this is a fast
unit test. End-to-end integration coverage rides on the existing
dispatcher integration tests + the new optout integration tests.
"""

from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone

from app.db.models import ScheduledEventStatus
from services.scheduler import dispatcher


def _event(
    *,
    event_type: str = "dose_due",
    patient_id: str = "9100",
    payload: dict | None = None,
) -> types.SimpleNamespace:
    """Stand-in for a ScheduledEvent. Just enough fields for the
    dispatcher's gate + freshness checks. We don't go beyond the
    gate in these tests because the patient is opted out."""
    return types.SimpleNamespace(
        id=1,
        event_type=event_type,
        patient_id=patient_id,
        scheduled_for=datetime.now(timezone.utc) - timedelta(minutes=1),
        payload=payload or {},
        status=ScheduledEventStatus.pending,
    )


async def test_optout_gate_skips_dose_event(monkeypatch):
    """A dose_due event for an opted-out patient must short-circuit
    BEFORE _build_dose_reminder runs. The skip prefix lets the
    scheduler mark the event as ``skipped`` rather than ``failed``."""

    async def fake_get_by_phone(_db, phone):
        return types.SimpleNamespace(
            id=1, phone=phone, consent_sms=False
        )

    monkeypatch.setattr(
        dispatcher.patients_repo, "get_by_phone", fake_get_by_phone
    )

    # Mocked DB session — gate doesn't actually use it for anything
    # other than passing to the repo helper, which is itself mocked.
    fake_db = types.SimpleNamespace()

    out = await dispatcher.dispatch(_event(), db=fake_db)
    assert out is not None
    assert out.startswith("not_applicable:opted_out:")
    assert "dose_due" in out


async def test_optout_gate_skips_appointment_reminder(monkeypatch):
    """Same gate fires for any event_type — the suppression is per-
    patient, not per-event."""

    async def fake_get_by_phone(_db, phone):
        return types.SimpleNamespace(
            id=1, phone=phone, consent_sms=False
        )

    monkeypatch.setattr(
        dispatcher.patients_repo, "get_by_phone", fake_get_by_phone
    )

    out = await dispatcher.dispatch(
        _event(event_type="appointment_reminder_24h"),
        db=types.SimpleNamespace(),
    )
    assert out is not None
    assert out.startswith("not_applicable:opted_out:")
    assert "appointment_reminder_24h" in out


async def test_consenting_patient_not_blocked_by_helper(monkeypatch):
    """The helper must return False for a patient with consent_sms=True.
    Tested at the helper level — going further requires a real DB
    session, which the dispatcher integration tests already exercise."""

    async def fake_get_by_phone(_db, phone):
        return types.SimpleNamespace(
            id=1, phone=phone, consent_sms=True
        )

    monkeypatch.setattr(
        dispatcher.patients_repo, "get_by_phone", fake_get_by_phone
    )

    blocked = await dispatcher._patient_opted_out(
        types.SimpleNamespace(), "9100"
    )
    assert blocked is False


async def test_helper_returns_false_for_missing_patient(monkeypatch):
    """If the patient row is missing the gate must NOT block the
    dispatch — we'd rather the dispatcher proceed and surface a
    different error than silently suppress a real send."""

    async def fake_get_by_phone(_db, phone):
        return None

    monkeypatch.setattr(
        dispatcher.patients_repo, "get_by_phone", fake_get_by_phone
    )

    blocked = await dispatcher._patient_opted_out(
        types.SimpleNamespace(), "9999"
    )
    assert blocked is False


async def test_helper_returns_false_for_empty_phone(monkeypatch):
    """No phone → no gate check → no network round-trip. Avoids
    spurious DB calls when the dispatcher receives a malformed
    event."""
    blocked = await dispatcher._patient_opted_out(
        types.SimpleNamespace(), ""
    )
    assert blocked is False

"""Unit tests for the dispatcher's bot-pause + opt-out compliance gate.

The dispatcher routes EVERY scheduled event through
``_patient_outbound_blocked`` before building any message. Two
distinct block sources:

    "opted_out": ``consent_sms = False`` (patient sent STOP)
    "bot_paused": ``bot_paused_at IS NOT NULL`` (ops set the brake)

Both produce a ``not_applicable:{reason}:{event_type}`` skip prefix
the scheduler treats as success-with-no-action. Tests confirm both
sources fire, are distinguishable in the prefix, and that the legacy
``_patient_opted_out`` alias still narrows correctly to the opt-out
source only.
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
):
    return types.SimpleNamespace(
        id=1,
        event_type=event_type,
        patient_id=patient_id,
        scheduled_for=datetime.now(timezone.utc) - timedelta(minutes=1),
        payload=payload or {},
        status=ScheduledEventStatus.pending,
    )


def _patient(
    *,
    consent_sms: bool = True,
    bot_paused_at: datetime | None = None,
):
    return types.SimpleNamespace(
        id=1,
        phone="9100",
        consent_sms=consent_sms,
        bot_paused_at=bot_paused_at,
        bot_paused_reason=None,
        bot_paused_by=None,
    )


# ---- _patient_outbound_blocked --------------------------------------------


async def test_helper_returns_none_for_consenting_active_patient(monkeypatch):
    """consent_sms=True + bot_paused_at IS NULL → None (proceed)."""

    async def fake_get(_db, phone):
        return _patient(consent_sms=True, bot_paused_at=None)

    monkeypatch.setattr(
        dispatcher.patients_repo, "get_by_phone", fake_get
    )

    out = await dispatcher._patient_outbound_blocked(
        types.SimpleNamespace(), "9100"
    )
    assert out is None


async def test_helper_returns_opted_out_when_consent_revoked(monkeypatch):
    async def fake_get(_db, phone):
        return _patient(consent_sms=False)

    monkeypatch.setattr(
        dispatcher.patients_repo, "get_by_phone", fake_get
    )

    out = await dispatcher._patient_outbound_blocked(
        types.SimpleNamespace(), "9100"
    )
    assert out == "opted_out"


async def test_helper_returns_bot_paused_when_pause_set(monkeypatch):
    """consent_sms is unchanged but bot_paused_at is set →
    ``"bot_paused"``. The two sources MUST be distinguishable so
    the audit trail and dashboard render them differently."""
    paused = datetime.now(timezone.utc) - timedelta(minutes=5)

    async def fake_get(_db, phone):
        return _patient(consent_sms=True, bot_paused_at=paused)

    monkeypatch.setattr(
        dispatcher.patients_repo, "get_by_phone", fake_get
    )

    out = await dispatcher._patient_outbound_blocked(
        types.SimpleNamespace(), "9100"
    )
    assert out == "bot_paused"


async def test_helper_opt_out_takes_precedence_over_pause(monkeypatch):
    """Both consent_sms=False AND bot_paused_at set → opted_out
    wins. The patient explicitly revoking consent is the stronger
    signal and we want the audit trail to show that, not just
    the ops pause."""
    paused = datetime.now(timezone.utc) - timedelta(minutes=5)

    async def fake_get(_db, phone):
        return _patient(consent_sms=False, bot_paused_at=paused)

    monkeypatch.setattr(
        dispatcher.patients_repo, "get_by_phone", fake_get
    )

    out = await dispatcher._patient_outbound_blocked(
        types.SimpleNamespace(), "9100"
    )
    assert out == "opted_out"


async def test_helper_returns_none_for_missing_patient(monkeypatch):
    """No patient row → don't block. Better to surface a different
    failure mode (the dispatcher's downstream message-building will
    log the issue) than to silently suppress a real send."""

    async def fake_get(_db, phone):
        return None

    monkeypatch.setattr(
        dispatcher.patients_repo, "get_by_phone", fake_get
    )

    out = await dispatcher._patient_outbound_blocked(
        types.SimpleNamespace(), "missing"
    )
    assert out is None


async def test_helper_returns_none_for_empty_phone(monkeypatch):
    """No phone → no DB round-trip. Avoids spurious queries when
    the dispatcher receives a malformed event."""
    out = await dispatcher._patient_outbound_blocked(
        types.SimpleNamespace(), ""
    )
    assert out is None


# ---- Legacy _patient_opted_out alias --------------------------------------


async def test_legacy_alias_only_narrows_to_opt_out(monkeypatch):
    """The old ``_patient_opted_out`` helper is still imported by
    older test fixtures. It must keep returning True ONLY for the
    opt-out source — pause must NOT show up here, otherwise older
    tests would mistakenly assume pause = opt-out."""
    paused = datetime.now(timezone.utc)

    async def fake_get(_db, phone):
        return _patient(consent_sms=True, bot_paused_at=paused)

    monkeypatch.setattr(
        dispatcher.patients_repo, "get_by_phone", fake_get
    )

    # consent_sms=True + paused → blocked, but NOT via opt-out.
    assert await dispatcher._patient_opted_out(
        types.SimpleNamespace(), "9100"
    ) is False


# ---- dispatch() gate integration ------------------------------------------


async def test_dispatch_skips_paused_patient_with_bot_paused_prefix(monkeypatch):
    """End-to-end through ``dispatch()``: a paused patient produces
    a ``not_applicable:bot_paused:{event_type}`` prefix. The prefix
    is what the scheduler's ``_SKIPPED_PREFIXES`` matcher reads to
    mark the event as skipped (not failed). A wrong prefix here
    would either fail-fast OR conflate with opt-out in audit logs."""
    paused = datetime.now(timezone.utc) - timedelta(hours=1)

    async def fake_get(_db, phone):
        return _patient(consent_sms=True, bot_paused_at=paused)

    monkeypatch.setattr(
        dispatcher.patients_repo, "get_by_phone", fake_get
    )

    out = await dispatcher.dispatch(
        _event(event_type="dose_due"),
        db=types.SimpleNamespace(),
    )
    assert out is not None
    assert out.startswith("not_applicable:bot_paused:")
    assert "dose_due" in out


async def test_dispatch_skips_opted_out_patient_with_opted_out_prefix(monkeypatch):
    async def fake_get(_db, phone):
        return _patient(consent_sms=False)

    monkeypatch.setattr(
        dispatcher.patients_repo, "get_by_phone", fake_get
    )

    out = await dispatcher.dispatch(
        _event(event_type="appointment_reminder_24h"),
        db=types.SimpleNamespace(),
    )
    assert out is not None
    assert out.startswith("not_applicable:opted_out:")

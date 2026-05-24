"""Unit tests for the pregnancy milestone materializer's scheduling logic.

The DB + enqueue are mocked so we can assert the pure scheduling decisions:
future-only filtering, the weekly rolling horizon, and idempotent dedupe
against already-pending events. (DB-backed end-to-end behaviour is covered in
tests/integration/test_pregnancy.py.)
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from services.scheduler import pregnancy_milestones as pm

_NOW = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)  # ~ GA week 12
_LMP = date(2026, 1, 1)


@pytest.fixture()
def capture_enqueue(monkeypatch):
    """Patch the idempotent enqueue to record calls + return a lightweight row."""
    calls: list[SimpleNamespace] = []

    async def fake_enqueue(
        _db, *, event_type, patient_id, payload, idempotency_key, scheduled_for
    ):
        row = SimpleNamespace(
            event_type=event_type,
            patient_id=patient_id,
            payload=payload,
            idempotency_key=idempotency_key,
            scheduled_for=scheduled_for,
        )
        calls.append(row)
        return row

    monkeypatch.setattr(
        pm.scheduled_events_repo, "enqueue_idempotent", fake_enqueue
    )
    return calls


def _patch_pending(monkeypatch, rows):
    async def fake_list(_db, *, pregnancy_id):
        return [r for r in rows if (r.payload or {}).get("pregnancy_id") == pregnancy_id]

    monkeypatch.setattr(pm, "_list_pending_for_pregnancy", fake_list)


def _milestone_keys(calls):
    return {
        c.payload["milestone_key"]
        for c in calls
        if c.event_type == pm.PREGNANCY_MILESTONE_EVENT_TYPE
    }


def _weekly_weeks(calls):
    return {
        c.payload["ga_week"]
        for c in calls
        if c.event_type == pm.PREGNANCY_WEEKLY_EVENT_TYPE
    }


async def test_materialize_enqueues_future_only(monkeypatch, capture_enqueue):
    _patch_pending(monkeypatch, [])
    pregnancy = SimpleNamespace(id=1, patient_id=10, lmp_date=_LMP, edd=None)

    created = await pm.materialize_for_pregnancy(
        object(), pregnancy, patient_phone="p1", now=_NOW
    )

    keys = _milestone_keys(capture_enqueue)
    # Week-8 milestones are in the past relative to _NOW → not scheduled.
    assert "lab_booking" not in keys
    assert "scan_dating" not in keys
    # Week-20 milestone is in the future → scheduled.
    assert "scan_anomaly" in keys
    # Every enqueued event is scheduled strictly after now.
    assert all(c.scheduled_for > _NOW for c in created)
    # Weekly rolling horizon: the next two completed weeks (13, 14).
    assert _weekly_weeks(capture_enqueue) == {13, 14}


async def test_materialize_is_idempotent(monkeypatch, capture_enqueue):
    # Pretend scan_anomaly + the week-13 check-in are already queued.
    pending = [
        SimpleNamespace(
            event_type=pm.PREGNANCY_MILESTONE_EVENT_TYPE,
            payload={"pregnancy_id": 1, "milestone_key": "scan_anomaly"},
        ),
        SimpleNamespace(
            event_type=pm.PREGNANCY_WEEKLY_EVENT_TYPE,
            payload={"pregnancy_id": 1, "ga_week": 13},
        ),
    ]
    _patch_pending(monkeypatch, pending)
    pregnancy = SimpleNamespace(id=1, patient_id=10, lmp_date=_LMP, edd=None)

    await pm.materialize_for_pregnancy(
        object(), pregnancy, patient_phone="p1", now=_NOW
    )

    keys = _milestone_keys(capture_enqueue)
    assert "scan_anomaly" not in keys  # already pending → not re-enqueued
    assert "visit_20" in keys  # a different week-20 milestone still scheduled
    weeks = _weekly_weeks(capture_enqueue)
    assert 13 not in weeks  # deduped
    assert 14 in weeks


async def test_materialize_no_anchor_is_noop(monkeypatch, capture_enqueue):
    _patch_pending(monkeypatch, [])
    pregnancy = SimpleNamespace(id=2, patient_id=11, lmp_date=None, edd=None)

    created = await pm.materialize_for_pregnancy(
        object(), pregnancy, patient_phone="p2", now=_NOW
    )

    assert created == []
    assert capture_enqueue == []


async def test_materialize_derives_lmp_from_edd(monkeypatch, capture_enqueue):
    # Only EDD set — the engine derives LMP and still schedules.
    _patch_pending(monkeypatch, [])
    edd = date(2026, 10, 8)  # → LMP 2026-01-01
    pregnancy = SimpleNamespace(id=3, patient_id=12, lmp_date=None, edd=edd)

    created = await pm.materialize_for_pregnancy(
        object(), pregnancy, patient_phone="p3", now=_NOW
    )

    assert created  # non-empty
    assert "scan_anomaly" in _milestone_keys(capture_enqueue)

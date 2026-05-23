"""Unit tests for the missed-dose sweep + escalation module.

The repos are stubbed at the module's import boundary so this test runs
without a DB. Integration coverage (real Postgres) lives in
tests/integration/test_missed_doses_db.py.
"""

from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone


from app.db.models import AdherenceStatus
from services.scheduler import missed_doses


class _NoopAsyncSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None

    async def commit(self):
        return None

    async def flush(self):
        return None


def _adherence(*, id, status, scheduled_at, regimen_id=1, patient_id=2):
    return types.SimpleNamespace(
        id=id,
        status=status,
        scheduled_at=scheduled_at,
        regimen_id=regimen_id,
        patient_id=patient_id,
    )


def _regimen(id=1, patient_id=2):
    return types.SimpleNamespace(
        id=id,
        patient_id=patient_id,
        medication_name="Metformin",
        dose="500 mg",
    )


def _patient(id=2, phone="9100"):
    return types.SimpleNamespace(id=id, phone=phone)


def _stub_repos(
    monkeypatch,
    *,
    candidates,
    recent_per_regimen,
    regimen,
    patient,
    open_ticket=None,
):
    """Patch every repo function the module reaches for. Returns a dict of
    captured calls for assertions."""
    captured = {"missed": [], "tickets": []}

    async def list_pending_past(_db, *, older_than, limit=200):
        return list(candidates)

    async def mark_missed(_db, _id, **kwargs):
        captured["missed"].append((_id, kwargs))
        # Mutate the source candidate so subsequent list_recent_for_regimen
        # sees status=missed (mirrors real DB behaviour).
        for c in candidates:
            if c.id == _id:
                c.status = AdherenceStatus.missed
        return None

    async def list_recent_for_regimen(_db, regimen_id, *, limit=5, up_to=None):
        # `up_to` is consulted by the production code to filter out future
        # materialized rows; in tests we pre-populate `recent_per_regimen`
        # with the desired view so we ignore it.
        return list(recent_per_regimen.get(regimen_id, []))

    async def get_regimen(_db, _id):
        return regimen

    async def get_patient(_db, _id):
        return patient

    async def find_open(_db, *, patient_id, category):
        return open_ticket

    async def create_ticket(_db, **kwargs):
        ticket = types.SimpleNamespace(id=999, **kwargs)
        captured["tickets"].append(kwargs)
        return ticket

    monkeypatch.setattr(
        missed_doses.adherence_events_repo,
        "list_pending_past",
        list_pending_past,
    )
    monkeypatch.setattr(
        missed_doses.adherence_events_repo, "mark_missed", mark_missed
    )
    monkeypatch.setattr(
        missed_doses.adherence_events_repo,
        "list_recent_for_regimen",
        list_recent_for_regimen,
    )
    monkeypatch.setattr(missed_doses.regimens_repo, "get", get_regimen)
    monkeypatch.setattr(missed_doses.patients_repo, "get", get_patient)
    monkeypatch.setattr(
        missed_doses.ops_tickets_repo,
        "find_open_for_patient_category",
        find_open,
    )
    monkeypatch.setattr(missed_doses.ops_tickets_repo, "create", create_ticket)
    return captured


def _past(minutes_ago: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)


async def test_sweep_marks_past_due_as_missed(monkeypatch):
    candidates = [
        _adherence(id=1, status=AdherenceStatus.scheduled, scheduled_at=_past(120)),
        _adherence(id=2, status=AdherenceStatus.scheduled, scheduled_at=_past(150)),
    ]
    captured = _stub_repos(
        monkeypatch,
        candidates=candidates,
        recent_per_regimen={1: candidates},  # only 2 misses → no escalation
        regimen=_regimen(),
        patient=_patient(),
    )

    out = await missed_doses.sweep_and_escalate(_NoopAsyncSession())
    assert out["candidates_examined"] == 2
    assert out["marked_missed"] == 2
    assert out["escalated"] == 0
    assert {c[0] for c in captured["missed"]} == {1, 2}
    assert captured["tickets"] == []


async def test_sweep_escalates_after_threshold_consecutive_misses(monkeypatch):
    """With 3 consecutive misses on the same regimen, an ops_ticket fires."""
    # Three brand-new misses on regimen 1.
    candidates = [
        _adherence(id=10, status=AdherenceStatus.scheduled, scheduled_at=_past(95)),
        _adherence(id=11, status=AdherenceStatus.scheduled, scheduled_at=_past(120)),
        _adherence(id=12, status=AdherenceStatus.scheduled, scheduled_at=_past(150)),
    ]
    # After mark_missed mutates them, list_recent_for_regimen will see all
    # three as missed (sorted newest first by scheduled_at).
    recent_for_1 = sorted(candidates, key=lambda e: e.scheduled_at, reverse=True)
    captured = _stub_repos(
        monkeypatch,
        candidates=candidates,
        recent_per_regimen={1: recent_for_1},
        regimen=_regimen(id=1, patient_id=2),
        patient=_patient(id=2, phone="9100"),
    )

    out = await missed_doses.sweep_and_escalate(_NoopAsyncSession())
    assert out["marked_missed"] == 3
    assert out["escalated"] == 1
    assert len(captured["tickets"]) == 1
    ticket = captured["tickets"][0]
    assert ticket["patient_id"] == "9100"
    assert ticket["category"] == "missed_doses"
    assert "Metformin" in ticket["notes"]


async def test_sweep_does_not_escalate_when_recent_includes_a_taken(monkeypatch):
    """Even with one fresh miss, if the previous dose was Taken the streak
    is broken — no escalation."""
    candidates = [
        _adherence(id=20, status=AdherenceStatus.scheduled, scheduled_at=_past(95)),
    ]
    # Recent: [missed (just-now), missed, TAKEN] → no streak of 3 misses.
    recent_for_1 = [
        _adherence(id=20, status=AdherenceStatus.missed, scheduled_at=_past(95)),
        _adherence(id=19, status=AdherenceStatus.missed, scheduled_at=_past(720)),
        _adherence(id=18, status=AdherenceStatus.taken, scheduled_at=_past(1440)),
    ]
    captured = _stub_repos(
        monkeypatch,
        candidates=candidates,
        recent_per_regimen={1: recent_for_1},
        regimen=_regimen(),
        patient=_patient(),
    )

    out = await missed_doses.sweep_and_escalate(_NoopAsyncSession())
    assert out["marked_missed"] == 1
    assert out["escalated"] == 0
    assert captured["tickets"] == []


async def test_sweep_avoids_duplicate_tickets(monkeypatch):
    """When an open missed_doses ticket already exists for the patient, don't
    spam another one. The misses are still marked, escalation count stays 0."""
    candidates = [
        _adherence(id=30, status=AdherenceStatus.scheduled, scheduled_at=_past(95)),
        _adherence(id=31, status=AdherenceStatus.scheduled, scheduled_at=_past(120)),
        _adherence(id=32, status=AdherenceStatus.scheduled, scheduled_at=_past(150)),
    ]
    recent = sorted(candidates, key=lambda e: e.scheduled_at, reverse=True)
    existing_open_ticket = types.SimpleNamespace(id=42)

    captured = _stub_repos(
        monkeypatch,
        candidates=candidates,
        recent_per_regimen={1: recent},
        regimen=_regimen(),
        patient=_patient(),
        open_ticket=existing_open_ticket,
    )

    out = await missed_doses.sweep_and_escalate(_NoopAsyncSession())
    assert out["marked_missed"] == 3
    assert out["escalated"] == 0
    assert captured["tickets"] == []


async def test_sweep_with_no_candidates_is_noop(monkeypatch):
    captured = _stub_repos(
        monkeypatch,
        candidates=[],
        recent_per_regimen={},
        regimen=_regimen(),
        patient=_patient(),
    )

    out = await missed_doses.sweep_and_escalate(_NoopAsyncSession())
    assert out == {
        "candidates_examined": 0,
        "marked_missed": 0,
        "regimens_checked": 0,
        "escalated": 0,
    }
    assert captured["missed"] == []
    assert captured["tickets"] == []

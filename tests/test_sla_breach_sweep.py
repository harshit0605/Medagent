"""Unit tests for the SLA breach sweep.

The repo layer is stubbed at the module boundary so the sweep runs
without a real DB. Integration coverage (round-trip through Postgres
via the OpsTicket model) lives in
tests/integration/test_sla_breach_sweep_db.py.

We're asserting the sweep's contract:

    - Tickets in the candidate list get marked exactly once per pass.
    - The same ticket appearing in two consecutive passes only counts
      as breached on the first one (idempotency via the repo layer).
    - The returned counter dict groups breaches by category — that's
      what the scheduler logs as a heartbeat detail and what future
      analytics queries will mirror.
"""

from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone

from services.scheduler import sla_breach_sweep


def _ticket(
    *,
    id: int,
    category: str = "missed_dose",
    sla_minutes: int = 60,
    created_at: datetime | None = None,
    sla_breached_at: datetime | None = None,
    patient_id: str = "9100",
):
    return types.SimpleNamespace(
        id=id,
        category=category,
        sla_minutes=sla_minutes,
        created_at=created_at or (datetime.now(timezone.utc) - timedelta(hours=2)),
        sla_breached_at=sla_breached_at,
        patient_id=patient_id,
    )


def _patch(monkeypatch, *, candidates, marked_pre_existing: set[int] | None = None):
    """Stub ops_tickets_repo.find_breach_candidates +
    ops_tickets_repo.mark_sla_breached at the module's import boundary.

    ``marked_pre_existing`` simulates the idempotency case: tickets
    whose ``sla_breached_at`` is already set get returned unchanged
    by ``mark_sla_breached`` (no new stamp, no counter bump).
    """
    state = {"marks": []}

    async def find_candidates(_db, *, now):
        return candidates

    async def mark_breached(_db, ticket_id, *, when):
        target = next((t for t in candidates if t.id == ticket_id), None)
        if target is None:
            return None
        if target.id in (marked_pre_existing or set()):
            # Idempotent path: row already had ``sla_breached_at`` set
            # before the sweep saw it. Repo returns it unchanged.
            return target
        target.sla_breached_at = when
        state["marks"].append((ticket_id, when))
        return target

    monkeypatch.setattr(
        sla_breach_sweep.ops_tickets_repo,
        "find_breach_candidates",
        find_candidates,
    )
    monkeypatch.setattr(
        sla_breach_sweep.ops_tickets_repo,
        "mark_sla_breached",
        mark_breached,
    )
    return state


async def test_sweep_no_candidates_returns_zero_counts(monkeypatch):
    _patch(monkeypatch, candidates=[])
    out = await sla_breach_sweep.sweep_sla_breaches(_FakeSession())
    assert out == {
        "candidates": 0,
        "breached": 0,
        "breached_ticket_ids": [],
        "breached_by_category": {},
    }


async def test_sweep_marks_each_candidate(monkeypatch):
    """One ticket → one mark + one counter. The category histogram is
    keyed by ticket.category so analytics can partition breaches."""
    candidates = [
        _ticket(id=1, category="missed_dose"),
        _ticket(id=2, category="onboarding_stuck"),
        _ticket(id=3, category="missed_dose"),
    ]
    state = _patch(monkeypatch, candidates=candidates)
    out = await sla_breach_sweep.sweep_sla_breaches(_FakeSession())
    assert out["candidates"] == 3
    assert out["breached"] == 3
    assert sorted(out["breached_ticket_ids"]) == [1, 2, 3]
    assert out["breached_by_category"] == {
        "missed_dose": 2,
        "onboarding_stuck": 1,
    }
    # mark_sla_breached called once per candidate.
    assert len(state["marks"]) == 3


async def test_sweep_idempotent_when_repo_treats_as_already_marked(monkeypatch):
    """A ticket that the repo treats as pre-marked (its
    ``sla_breached_at`` was set since the candidate fetch) must NOT
    bump the breach counter — the sweep counter only credits NEW
    marks. Prevents double-counting if two replicas race."""
    pre_existing_stamp = datetime.now(timezone.utc) - timedelta(minutes=5)
    candidates = [
        _ticket(id=1, category="missed_dose"),
        _ticket(
            id=2,
            category="missed_dose",
            sla_breached_at=pre_existing_stamp,
        ),
    ]
    _patch(monkeypatch, candidates=candidates, marked_pre_existing={2})
    out = await sla_breach_sweep.sweep_sla_breaches(_FakeSession())
    # 2 candidates fetched, but only 1 actually NEW mark.
    assert out["candidates"] == 2
    assert out["breached"] == 1
    assert out["breached_ticket_ids"] == [1]
    assert out["breached_by_category"] == {"missed_dose": 1}


async def test_sweep_returns_categories_per_ticket(monkeypatch):
    """The category histogram is what the scheduler heartbeat carries —
    confirm grouping is exact (no 'unknown' bucket, no losses)."""
    candidates = [
        _ticket(id=1, category="onboarding_stuck"),
        _ticket(id=2, category="onboarding_stuck"),
        _ticket(id=3, category="onboarding_stuck"),
        _ticket(id=4, category="refill_help"),
    ]
    _patch(monkeypatch, candidates=candidates)
    out = await sla_breach_sweep.sweep_sla_breaches(_FakeSession())
    assert out["breached_by_category"] == {
        "onboarding_stuck": 3,
        "refill_help": 1,
    }


async def test_sweep_uses_supplied_now_for_stamp(monkeypatch):
    """When the caller passes an explicit ``now``, every breach mark
    must use that timestamp — important for deterministic tests and
    for backfill jobs that run with a frozen reference time."""
    fixed = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)
    candidates = [_ticket(id=1, category="missed_dose")]
    state = _patch(monkeypatch, candidates=candidates)
    out = await sla_breach_sweep.sweep_sla_breaches(
        _FakeSession(), now=fixed
    )
    assert out["breached"] == 1
    assert state["marks"] == [(1, fixed)]


# Trivial async-context-manager stand-in — the sweep treats the session
# as opaque and only passes it through to the repo helpers. Both helpers
# are mocked so the session never gets used.
class _FakeSession:
    pass

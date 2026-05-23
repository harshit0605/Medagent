"""Unit tests for the adherence-drop alerter.

The DB-side aggregation is mocked at the module's import boundary
so we exercise the policy logic (threshold + hysteresis +
min-volume gate + idempotency) independently of Postgres.
Integration coverage with a real adherence_events + ops_tickets
flow lives in tests/integration/test_adherence_pattern_sweep.py.

Contract under test:

    1. ``rate < threshold`` AND ``completed >= MIN_SCHEDULED``
       AND no existing ticket → open ONE ticket per patient.
    2. Same condition with existing ticket → re-note (no duplicate).
    3. ``rate >= recovery_threshold`` AND existing ticket →
       auto-resolve.
    4. Hysteresis band (between threshold + recovery): no opens
       for fresh patients, no resolves for existing tickets.
    5. ``completed < MIN_SCHEDULED`` → skip (no opens, no
       resolves; clock pauses for quiet patients).
"""

from __future__ import annotations

import types

from app.db.models import AdherenceStatus
from services.scheduler import adherence_pattern_sweep as sweep


# ---- _compute_rate -------------------------------------------------------


def test_compute_rate_basic():
    rate, completed = sweep._compute_rate(taken=8, missed=2, skipped=0)
    assert completed == 10
    assert rate == 0.8


def test_compute_rate_zero_completed_returns_zero():
    """A patient with no doses in the window shouldn't divide by
    zero — return ``(0.0, 0)`` so the caller's min-volume gate
    can short-circuit cleanly."""
    rate, completed = sweep._compute_rate(taken=0, missed=0, skipped=0)
    assert rate == 0.0
    assert completed == 0


def test_compute_rate_skipped_counts_as_non_taken():
    """Skipped + missed both count as non-adherent; only ``taken``
    is in the numerator. Matches the existing
    _adherence_summary semantics in main.py."""
    rate, completed = sweep._compute_rate(taken=3, missed=2, skipped=2)
    # 3 / (3 + 2 + 2) = 0.428...
    assert completed == 7
    assert rate == round(3 / 7, 3)


# ---- Sweep policy helpers ------------------------------------------------


def _make_aggregation_rows(
    *, patient_id: int, phone: str, taken: int, missed: int, skipped: int
) -> list[tuple]:
    """Mirror the (patient_id, phone, status, count) shape the SQL
    aggregation returns. Used by the per-test mock."""
    rows = []
    if taken:
        rows.append(
            (patient_id, phone, AdherenceStatus.taken, taken)
        )
    if missed:
        rows.append(
            (patient_id, phone, AdherenceStatus.missed, missed)
        )
    if skipped:
        rows.append(
            (patient_id, phone, AdherenceStatus.skipped, skipped)
        )
    return rows


def _patch(
    monkeypatch,
    *,
    rows: list[tuple],
    open_tickets: dict[str, object] | None = None,
):
    """Stub the SQL execute + ops_tickets repo so tests can drive
    the sweep without a DB."""
    state = {
        "open_tickets": dict(open_tickets or {}),
        "creates": [],
        "resolves": [],
        "appends": [],
    }

    class _StubResult:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class _StubSession:
        async def execute(self, _stmt):
            return _StubResult(rows)

    # Replace the module's ops_tickets_repo helpers.
    async def fake_find_open(_db, *, patient_id, category):
        return state["open_tickets"].get(patient_id)

    async def fake_create(
        _db,
        *,
        patient_id,
        category,
        priority,
        sla_minutes,
        notes=None,
    ):
        ticket = types.SimpleNamespace(
            id=len(state["creates"]) + 100,
            patient_id=patient_id,
            category=category,
            priority=priority,
            sla_minutes=sla_minutes,
            notes=notes,
        )
        state["creates"].append(ticket)
        state["open_tickets"][patient_id] = ticket
        return ticket

    async def fake_resolve(_db, ticket_id, *, at=None, actor="ops", notes=None):
        state["resolves"].append((ticket_id, notes))
        for pid, t in list(state["open_tickets"].items()):
            if t.id == ticket_id:
                del state["open_tickets"][pid]
                break
        return types.SimpleNamespace(id=ticket_id, notes=notes)

    async def fake_append_note(_db, ticket_id, *, actor, note):
        state["appends"].append((ticket_id, actor, note))
        return types.SimpleNamespace(id=ticket_id)

    monkeypatch.setattr(
        sweep.ops_tickets_repo,
        "find_open_for_patient_category",
        fake_find_open,
    )
    monkeypatch.setattr(
        sweep.ops_tickets_repo, "create", fake_create
    )
    monkeypatch.setattr(
        sweep.ops_tickets_repo, "resolve", fake_resolve
    )
    monkeypatch.setattr(
        sweep.ops_tickets_repo, "append_note", fake_append_note
    )
    return _StubSession(), state


# ---- Drop opens ticket ---------------------------------------------------


async def test_drop_below_threshold_with_volume_opens_ticket(monkeypatch):
    """3 of 10 doses taken (30% rate) with completed=10 ≥ floor →
    open one ticket. Notes carry the rate + counts so a doctor can
    triage from the queue."""
    rows = _make_aggregation_rows(
        patient_id=1, phone="9100", taken=3, missed=5, skipped=2
    )
    db, state = _patch(monkeypatch, rows=rows)

    out = await sweep.sweep_adherence_drops(db)

    assert out["opened"] == 1
    assert out["opened_patient_ids"] == [1]
    assert len(state["creates"]) == 1
    ticket = state["creates"][0]
    assert ticket.patient_id == "9100"
    assert ticket.category == sweep.CATEGORY
    assert ticket.priority == sweep.PRIORITY
    assert ticket.sla_minutes == sweep.SLA_MINUTES
    # Notes carry the rate so ops can scan from the queue.
    assert "30.0%" in (ticket.notes or "")
    assert "10" in (ticket.notes or "")  # completed count


async def test_volume_below_floor_does_not_open(monkeypatch):
    """1 of 3 doses taken (33% rate) is below threshold but the
    completed count (3) is below the min-volume floor (default 7).
    Suppress: a patient with 3 doses in 7 days is too thin a
    signal to alarm a doctor."""
    rows = _make_aggregation_rows(
        patient_id=1, phone="9100", taken=1, missed=2, skipped=0
    )
    db, state = _patch(monkeypatch, rows=rows)

    out = await sweep.sweep_adherence_drops(db)

    assert out["opened"] == 0
    assert out["skipped_low_volume"] == 1
    assert state["creates"] == []


async def test_existing_ticket_re_notes_no_duplicate(monkeypatch):
    """Subsequent sweep while still depressed → re-note the
    existing ticket with the latest rate. Never open a duplicate
    — the queue would balloon."""
    existing = types.SimpleNamespace(
        id=999,
        patient_id="9100",
        category=sweep.CATEGORY,
    )
    rows = _make_aggregation_rows(
        patient_id=1, phone="9100", taken=2, missed=6, skipped=0
    )
    db, state = _patch(
        monkeypatch, rows=rows, open_tickets={"9100": existing}
    )

    out = await sweep.sweep_adherence_drops(db)

    assert out["opened"] == 0
    assert out["re_noted"] == 1
    assert state["creates"] == []
    assert len(state["appends"]) == 1
    ticket_id, actor, note = state["appends"][0]
    assert ticket_id == 999
    assert actor == "system"
    assert "still depressed" in note


# ---- Recovery auto-resolves ---------------------------------------------


async def test_recovery_with_open_ticket_auto_resolves(monkeypatch):
    """Patient recovered to 80% (above default 75% recovery
    threshold) and has an open ticket → resolve it. Without this,
    a patient who sorted things out would have a stale ticket
    cluttering the doctor's digest."""
    existing = types.SimpleNamespace(
        id=999, patient_id="9100", category=sweep.CATEGORY
    )
    rows = _make_aggregation_rows(
        patient_id=1, phone="9100", taken=8, missed=2, skipped=0
    )
    db, state = _patch(
        monkeypatch, rows=rows, open_tickets={"9100": existing}
    )

    out = await sweep.sweep_adherence_drops(db)

    assert out["auto_resolved"] == 1
    assert out["auto_resolved_patient_ids"] == [1]
    assert len(state["resolves"]) == 1
    ticket_id, notes = state["resolves"][0]
    assert ticket_id == 999
    assert "auto-resolved" in notes
    assert "80.0%" in notes


async def test_recovery_without_open_ticket_is_noop(monkeypatch):
    """High adherence rate + no existing ticket → do nothing.
    Sweep must NOT try to resolve a non-existent ticket."""
    rows = _make_aggregation_rows(
        patient_id=1, phone="9100", taken=8, missed=2, skipped=0
    )
    db, state = _patch(monkeypatch, rows=rows)

    out = await sweep.sweep_adherence_drops(db)

    assert out["opened"] == 0
    assert out["auto_resolved"] == 0
    assert state["creates"] == []
    assert state["resolves"] == []


# ---- Hysteresis band -----------------------------------------------------


async def test_hysteresis_band_no_existing_ticket_is_noop(monkeypatch):
    """65% rate sits between the 60% drop threshold and 75%
    recovery threshold. With no open ticket, the sweep must NOT
    open one (rate hasn't crossed the open boundary). The
    hysteresis band prevents flicker."""
    rows = _make_aggregation_rows(
        patient_id=1, phone="9100", taken=13, missed=5, skipped=2
    )
    # 13 / 20 = 0.65
    db, state = _patch(monkeypatch, rows=rows)

    out = await sweep.sweep_adherence_drops(db)

    assert out["opened"] == 0
    assert out["auto_resolved"] == 0
    assert state["creates"] == []


async def test_hysteresis_band_with_open_ticket_keeps_it_open(monkeypatch):
    """Same 65% rate WITH an existing ticket → leave the ticket
    alone. Only auto-resolve once the rate fully recovers (≥ 75%),
    not when it's merely above the open threshold."""
    existing = types.SimpleNamespace(
        id=999, patient_id="9100", category=sweep.CATEGORY
    )
    rows = _make_aggregation_rows(
        patient_id=1, phone="9100", taken=13, missed=5, skipped=2
    )
    db, state = _patch(
        monkeypatch, rows=rows, open_tickets={"9100": existing}
    )

    out = await sweep.sweep_adherence_drops(db)

    assert out["auto_resolved"] == 0
    # Not below threshold either, so no "still depressed" re-note —
    # leave the timeline alone.
    assert state["resolves"] == []
    assert state["appends"] == []


# ---- Mixed pass over multiple patients -----------------------------------


async def test_mixed_pass_evaluates_each_patient_independently(
    monkeypatch,
):
    """Real-world pass: one patient dropping, one recovering, one
    quiet (below volume floor). Each must be evaluated
    independently — a single shared decision would be wrong."""
    dropping = _make_aggregation_rows(
        patient_id=1, phone="9100", taken=2, missed=6, skipped=2
    )  # 20%
    recovering = _make_aggregation_rows(
        patient_id=2, phone="9101", taken=9, missed=1, skipped=0
    )  # 90%
    quiet = _make_aggregation_rows(
        patient_id=3, phone="9102", taken=0, missed=2, skipped=0
    )  # below volume floor

    rows = dropping + recovering + quiet

    open_for_recovering = types.SimpleNamespace(
        id=200, patient_id="9101", category=sweep.CATEGORY
    )
    db, state = _patch(
        monkeypatch,
        rows=rows,
        open_tickets={"9101": open_for_recovering},
    )

    out = await sweep.sweep_adherence_drops(db)

    assert out["patients_evaluated"] == 3
    assert out["opened"] == 1
    assert out["opened_patient_ids"] == [1]
    assert out["auto_resolved"] == 1
    assert out["auto_resolved_patient_ids"] == [2]
    assert out["skipped_low_volume"] == 1

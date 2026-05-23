"""Unit tests for the delivery-failure-burst alert sweep.

The sweep glues two existing surfaces together — the
``delivery_summary`` rollup we built earlier this week, and the
ops_tickets repo. Repos are stubbed at the module's import boundary
so this is a fast unit test. Integration coverage lives in the
delivery-metrics integration tests.

Contract under test:

    1. Failure rate above ``DELIVERY_FAILURE_THRESHOLD`` AND volume
       at or above ``DELIVERY_ALERT_MIN_VOLUME`` → open exactly one
       ticket. Idempotent across passes (existing ticket → re-note
       only).
    2. Failure rate at or below ``DELIVERY_RECOVERY_THRESHOLD`` AND
       an open ticket exists → auto-resolve.
    3. Below ``MIN_VOLUME`` → no opens, no resolves (statistical
       noise floor; ride out the blip).
    4. In the hysteresis band (between recovery and failure
       thresholds) with no open ticket → no-op.
"""

from __future__ import annotations

import types

from services.scheduler import delivery_alert_sweep


def _summary(
    *,
    total: int,
    failure_rate: float,
    failed: int = 0,
    failed_pre_meta: int = 0,
    top_failure_codes: list[dict] | None = None,
) -> dict:
    """Build a ``delivery_summary`` dict in the shape the sweep
    consumes. Mirrors ``app.db.repositories.delivery_metrics`` output."""
    return {
        "since": "2026-05-07T00:00:00+00:00",
        "total_outbound": total,
        "by_status": {
            "delivered": max(0, total - failed - failed_pre_meta),
            "read": 0,
            "sent_only": 0,
            "failed": failed,
            "failed_pre_meta": failed_pre_meta,
            "no_status_yet": 0,
        },
        "delivery_rate": 1.0 - failure_rate if total else 0.0,
        "failure_rate": failure_rate,
        "by_payload_kind": {},
        "top_failure_codes": top_failure_codes or [],
    }


def _patch(
    monkeypatch,
    *,
    summary: dict,
    open_ticket=None,
):
    """Stub delivery_metrics_repo + ops_tickets_repo. Returns a
    captured-state dict the test inspects."""
    state = {
        "open_ticket": open_ticket,
        "creates": [],
        "resolves": [],
        "appends": [],
        "ticket_finds": [],
    }

    async def fake_summary(_db, *, since=None):
        return summary

    async def fake_find_open(_db, *, patient_id, category):
        state["ticket_finds"].append((patient_id, category))
        return state["open_ticket"]

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
        # Newly-created ticket becomes the "open ticket" so a
        # second pass within the same test sees it.
        state["open_ticket"] = ticket
        return ticket

    async def fake_resolve(_db, ticket_id, *, at=None, actor="ops", notes=None):
        state["resolves"].append((ticket_id, notes))
        state["open_ticket"] = None
        return types.SimpleNamespace(id=ticket_id, notes=notes)

    async def fake_append_note(_db, ticket_id, *, actor, note):
        state["appends"].append((ticket_id, actor, note))
        return types.SimpleNamespace(id=ticket_id)

    monkeypatch.setattr(
        delivery_alert_sweep.delivery_metrics_repo,
        "delivery_summary",
        fake_summary,
    )
    monkeypatch.setattr(
        delivery_alert_sweep.ops_tickets_repo,
        "find_open_for_patient_category",
        fake_find_open,
    )
    monkeypatch.setattr(
        delivery_alert_sweep.ops_tickets_repo, "create", fake_create
    )
    monkeypatch.setattr(
        delivery_alert_sweep.ops_tickets_repo, "resolve", fake_resolve
    )
    monkeypatch.setattr(
        delivery_alert_sweep.ops_tickets_repo,
        "append_note",
        fake_append_note,
    )
    return state


# ---- Open-on-burst --------------------------------------------------------


async def test_burst_above_threshold_with_volume_opens_ticket(monkeypatch):
    """20% failure rate over 10 sends with no existing ticket →
    create exactly one ticket with category outbound_delivery_burst,
    high priority, 60-min SLA."""
    state = _patch(
        monkeypatch,
        summary=_summary(
            total=10,
            failure_rate=0.2,
            failed=2,
            top_failure_codes=[
                {"code": 131047, "title": "Re-engagement", "count": 2}
            ],
        ),
    )

    out = await delivery_alert_sweep.sweep_delivery_alerts(_FakeSession())
    assert out["opened"] is True
    assert out["auto_resolved"] is False
    assert len(state["creates"]) == 1
    ticket = state["creates"][0]
    assert ticket.patient_id == delivery_alert_sweep.PATIENT_ID
    assert ticket.category == delivery_alert_sweep.CATEGORY
    assert ticket.priority == delivery_alert_sweep.PRIORITY
    assert ticket.sla_minutes == delivery_alert_sweep.SLA_MINUTES
    # Notes carry the metric so ops can triage from the queue.
    assert "20.0%" in (ticket.notes or "")
    assert "131047" in (ticket.notes or "")


async def test_burst_below_min_volume_does_not_open_ticket(monkeypatch):
    """1 of 1 sends failed (100% failure rate) but volume floor
    NOT met → no ticket. Single failed sends are noise, not signal."""
    state = _patch(
        monkeypatch,
        summary=_summary(total=1, failure_rate=1.0, failed=1),
    )
    out = await delivery_alert_sweep.sweep_delivery_alerts(_FakeSession())
    assert out["opened"] is False
    assert out["min_volume_skipped"] is True
    assert state["creates"] == []


async def test_burst_with_existing_ticket_appends_note_no_duplicate(monkeypatch):
    """Second pass while the alert is still firing must NOT create
    a duplicate ticket. Append a fresh note so ops sees the alarm
    is still live."""
    existing = types.SimpleNamespace(
        id=999,
        patient_id=delivery_alert_sweep.PATIENT_ID,
        category=delivery_alert_sweep.CATEGORY,
        sla_minutes=60,
    )
    state = _patch(
        monkeypatch,
        summary=_summary(total=20, failure_rate=0.25, failed=5),
        open_ticket=existing,
    )

    out = await delivery_alert_sweep.sweep_delivery_alerts(_FakeSession())
    assert out["opened"] is False
    assert state["creates"] == []
    # Note appended to the existing ticket.
    assert len(state["appends"]) == 1
    ticket_id, actor, note = state["appends"][0]
    assert ticket_id == 999
    assert actor == "system"
    assert "still elevated" in note
    assert "25.0%" in note


# ---- Auto-resolve on recovery -------------------------------------------


async def test_recovery_with_open_ticket_auto_resolves(monkeypatch):
    """Failure rate dropped to 2% (below recovery_threshold default
    of 5%) AND an open ticket exists → resolve it. Without auto-
    resolve, transient blips would leave stale tickets cluttering
    the queue."""
    existing = types.SimpleNamespace(
        id=999,
        patient_id=delivery_alert_sweep.PATIENT_ID,
        category=delivery_alert_sweep.CATEGORY,
        sla_minutes=60,
    )
    state = _patch(
        monkeypatch,
        summary=_summary(total=50, failure_rate=0.02, failed=1),
        open_ticket=existing,
    )

    out = await delivery_alert_sweep.sweep_delivery_alerts(_FakeSession())
    assert out["auto_resolved"] is True
    assert state["resolves"] == [
        (999, mock_resolve_note(state)),
    ]
    assert "auto-resolved" in state["resolves"][0][1]


async def test_recovery_without_open_ticket_is_noop(monkeypatch):
    """Healthy metrics with no open ticket → nothing to do. The
    sweep mustn't try to resolve a non-existent ticket."""
    state = _patch(
        monkeypatch,
        summary=_summary(total=50, failure_rate=0.02, failed=1),
    )
    out = await delivery_alert_sweep.sweep_delivery_alerts(_FakeSession())
    assert out["opened"] is False
    assert out["auto_resolved"] is False
    assert state["creates"] == []
    assert state["resolves"] == []


# ---- Hysteresis band ----------------------------------------------------


async def test_hysteresis_band_above_recovery_below_failure_is_noop(monkeypatch):
    """7% failure rate sits between the 5% recovery threshold and
    10% failure threshold. With no open ticket, the sweep must NOT
    open one (rate hasn't crossed open threshold) AND must NOT
    resolve anything. The hysteresis band prevents flicker."""
    state = _patch(
        monkeypatch,
        summary=_summary(total=50, failure_rate=0.07, failed=4),
    )
    out = await delivery_alert_sweep.sweep_delivery_alerts(_FakeSession())
    assert out["opened"] is False
    assert out["auto_resolved"] is False
    assert state["creates"] == []
    assert state["resolves"] == []


async def test_hysteresis_band_with_open_ticket_keeps_it_open(monkeypatch):
    """7% failure rate (above recovery threshold) AND an open ticket
    exists → leave the ticket alone. We only auto-resolve once
    metrics fully recover, not when they're merely elevated-but-
    not-critical."""
    existing = types.SimpleNamespace(
        id=999,
        patient_id=delivery_alert_sweep.PATIENT_ID,
        category=delivery_alert_sweep.CATEGORY,
        sla_minutes=60,
    )
    state = _patch(
        monkeypatch,
        summary=_summary(total=50, failure_rate=0.07, failed=4),
        open_ticket=existing,
    )
    out = await delivery_alert_sweep.sweep_delivery_alerts(_FakeSession())
    assert out["auto_resolved"] is False
    # Not above failure threshold either, so we don't append a
    # "still elevated" note — leave the timeline alone.
    assert state["resolves"] == []
    assert state["appends"] == []


# ---- Helpers ------------------------------------------------------------


def mock_resolve_note(state: dict) -> str:
    """Return the note string from the most recent resolve call. Used
    by the assertion above so we can match on substring without
    pinning the full note format."""
    if not state["resolves"]:
        return ""
    return state["resolves"][-1][1]


class _FakeSession:
    """Opaque session — every consumer is mocked."""

    pass

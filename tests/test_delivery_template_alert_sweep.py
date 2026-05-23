"""Unit tests for the per-template delivery-failure alert sweep.

The sweep glues two existing surfaces together — the
``delivery_summary_by_template`` rollup and the ops_tickets repo.
Repos are stubbed at the module's import boundary so this is a fast
unit test.

Contract under test:

    1. A template above ``FAILURE_THRESHOLD`` AND at or above
       ``MIN_VOLUME`` opens exactly one ticket. Subsequent passes
       while still elevated re-note rather than duplicating.
    2. A template at or below ``RECOVERY_THRESHOLD`` with an open
       ticket auto-resolves. Hysteresis between the two thresholds
       prevents flicker.
    3. A template below ``MIN_VOLUME`` is skipped — no opens, no
       resolves. The clock effectively pauses for quiet templates.
    4. The sweep walks ALL rows in the rollup; templates are
       independent (one failing template doesn't affect others).
"""

from __future__ import annotations

import types

from services.scheduler import delivery_template_alert_sweep


def _row(
    *,
    template_name: str,
    total: int,
    failure_rate: float = 0.0,
    failed: int = 0,
    failed_pre_meta: int = 0,
):
    """Build a delivery_summary_by_template-shaped row dict."""
    return {
        "template_name": template_name,
        "total": total,
        "delivered": max(0, total - failed - failed_pre_meta),
        "failed": failed,
        "failed_pre_meta": failed_pre_meta,
        "delivery_rate": 1.0 - failure_rate if total else 0.0,
        "failure_rate": failure_rate,
    }


def _patch(monkeypatch, *, rows, open_tickets: dict | None = None):
    """Stub delivery_metrics_repo.delivery_summary_by_template +
    ops_tickets_repo. ``open_tickets`` maps synthetic patient_id →
    SimpleNamespace pre-existing ticket so we can simulate
    "ticket already open" / "no ticket yet" per template."""
    state = {
        "creates": [],
        "resolves": [],
        "appends": [],
        "open_tickets": dict(open_tickets or {}),
    }

    async def fake_summary(_db, *, since=None):
        return rows

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
        # New ticket becomes the "open" entry so a later iteration
        # in the same pass sees it.
        state["open_tickets"][patient_id] = ticket
        return ticket

    async def fake_resolve(_db, ticket_id, *, at=None, actor="ops", notes=None):
        state["resolves"].append((ticket_id, notes))
        # Drop the resolved ticket from the open set.
        for pid, t in list(state["open_tickets"].items()):
            if t.id == ticket_id:
                del state["open_tickets"][pid]
                break
        return types.SimpleNamespace(id=ticket_id, notes=notes)

    async def fake_append_note(_db, ticket_id, *, actor, note):
        state["appends"].append((ticket_id, actor, note))
        return types.SimpleNamespace(id=ticket_id)

    monkeypatch.setattr(
        delivery_template_alert_sweep.delivery_metrics_repo,
        "delivery_summary_by_template",
        fake_summary,
    )
    monkeypatch.setattr(
        delivery_template_alert_sweep.ops_tickets_repo,
        "find_open_for_patient_category",
        fake_find_open,
    )
    monkeypatch.setattr(
        delivery_template_alert_sweep.ops_tickets_repo,
        "create",
        fake_create,
    )
    monkeypatch.setattr(
        delivery_template_alert_sweep.ops_tickets_repo,
        "resolve",
        fake_resolve,
    )
    monkeypatch.setattr(
        delivery_template_alert_sweep.ops_tickets_repo,
        "append_note",
        fake_append_note,
    )
    return state


# ---- Helpers --------------------------------------------------------------


class _FakeSession:
    pass


# ---- Open-on-burst -------------------------------------------------------


async def test_template_above_threshold_with_volume_opens_ticket(monkeypatch):
    """Single template at 25% failure with sufficient volume → open
    one ticket with the template-keyed synthetic patient_id and the
    documented category/priority/SLA."""
    state = _patch(
        monkeypatch,
        rows=[
            _row(
                template_name="dose_reminder_v2",
                total=20,
                failure_rate=0.25,
                failed=5,
            )
        ],
    )

    out = await delivery_template_alert_sweep.sweep_template_alerts(
        _FakeSession()
    )
    assert out["opened"] == 1
    assert out["opened_templates"] == ["dose_reminder_v2"]
    assert out["auto_resolved"] == 0
    assert len(state["creates"]) == 1
    ticket = state["creates"][0]
    assert ticket.patient_id == "platform:template:dose_reminder_v2"
    assert ticket.category == delivery_template_alert_sweep.CATEGORY
    assert ticket.priority == delivery_template_alert_sweep.PRIORITY
    assert ticket.sla_minutes == delivery_template_alert_sweep.SLA_MINUTES
    # Notes carry the template name + the failure metric so ops
    # can triage from the queue without drilling in.
    assert "dose_reminder_v2" in (ticket.notes or "")
    assert "25.0%" in (ticket.notes or "")


async def test_each_failing_template_gets_its_own_ticket(monkeypatch):
    """Two templates failing independently → two tickets. The
    per-template breakdown is what makes this slice valuable: each
    template gets its own lane in the queue, not a single
    aggregate."""
    state = _patch(
        monkeypatch,
        rows=[
            _row(
                template_name="dose_reminder_v2",
                total=10,
                failure_rate=0.30,
                failed=3,
            ),
            _row(
                template_name="appointment_reminder_v1",
                total=15,
                failure_rate=0.20,
                failed=3,
            ),
        ],
    )

    out = await delivery_template_alert_sweep.sweep_template_alerts(
        _FakeSession()
    )
    assert out["opened"] == 2
    assert sorted(out["opened_templates"]) == [
        "appointment_reminder_v1",
        "dose_reminder_v2",
    ]
    patient_ids = {t.patient_id for t in state["creates"]}
    assert patient_ids == {
        "platform:template:dose_reminder_v2",
        "platform:template:appointment_reminder_v1",
    }


async def test_template_below_min_volume_does_not_open(monkeypatch):
    """1-of-1 sends failed (100% failure rate) with volume below
    the 3-row floor → suppressed. One bad send isn't statistical
    signal."""
    state = _patch(
        monkeypatch,
        rows=[
            _row(
                template_name="lab_closure_update_v1",
                total=1,
                failure_rate=1.0,
                failed=1,
            )
        ],
    )

    out = await delivery_template_alert_sweep.sweep_template_alerts(
        _FakeSession()
    )
    assert out["opened"] == 0
    assert out["skipped_low_volume"] == 1
    assert state["creates"] == []


async def test_existing_ticket_appends_note_no_duplicate(monkeypatch):
    """Subsequent pass while still elevated → re-note the existing
    ticket. NEVER open a duplicate. Without idempotency, the ops
    queue would balloon with one ticket per sweep."""
    existing = types.SimpleNamespace(
        id=999,
        patient_id="platform:template:dose_reminder_v2",
        category=delivery_template_alert_sweep.CATEGORY,
        sla_minutes=60,
    )
    state = _patch(
        monkeypatch,
        rows=[
            _row(
                template_name="dose_reminder_v2",
                total=20,
                failure_rate=0.25,
                failed=5,
            )
        ],
        open_tickets={"platform:template:dose_reminder_v2": existing},
    )

    out = await delivery_template_alert_sweep.sweep_template_alerts(
        _FakeSession()
    )
    assert out["opened"] == 0
    assert out["re_noted"] == 1
    assert out["re_noted_templates"] == ["dose_reminder_v2"]
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
    """Failure rate dropped to 2% (below recovery threshold of 5%)
    AND template ticket exists → resolve it. Without auto-resolve,
    transient blips would leave stale per-template tickets stuck."""
    existing = types.SimpleNamespace(
        id=999,
        patient_id="platform:template:dose_reminder_v2",
        category=delivery_template_alert_sweep.CATEGORY,
        sla_minutes=60,
    )
    state = _patch(
        monkeypatch,
        rows=[
            _row(
                template_name="dose_reminder_v2",
                total=50,
                failure_rate=0.02,
                failed=1,
            )
        ],
        open_tickets={"platform:template:dose_reminder_v2": existing},
    )

    out = await delivery_template_alert_sweep.sweep_template_alerts(
        _FakeSession()
    )
    assert out["auto_resolved"] == 1
    assert out["auto_resolved_templates"] == ["dose_reminder_v2"]
    assert len(state["resolves"]) == 1
    ticket_id, notes = state["resolves"][0]
    assert ticket_id == 999
    assert "auto-resolved" in notes


async def test_recovery_without_open_ticket_is_noop(monkeypatch):
    """Healthy metrics with no open ticket → do nothing. Sweep must
    NOT try to resolve a non-existent ticket (would crash)."""
    state = _patch(
        monkeypatch,
        rows=[
            _row(
                template_name="dose_reminder_v2",
                total=50,
                failure_rate=0.02,
                failed=1,
            )
        ],
    )

    out = await delivery_template_alert_sweep.sweep_template_alerts(
        _FakeSession()
    )
    assert out["opened"] == 0
    assert out["auto_resolved"] == 0
    assert state["creates"] == []
    assert state["resolves"] == []


# ---- Hysteresis band ----------------------------------------------------


async def test_hysteresis_band_above_recovery_below_failure_is_noop(
    monkeypatch,
):
    """7% failure rate sits between the 5% recovery threshold and
    10% failure threshold. With no open ticket, the sweep must NOT
    open one (hasn't crossed open threshold) AND must NOT resolve
    anything. Hysteresis prevents flicker around the boundaries."""
    state = _patch(
        monkeypatch,
        rows=[
            _row(
                template_name="dose_reminder_v2",
                total=50,
                failure_rate=0.07,
                failed=4,
            )
        ],
    )

    out = await delivery_template_alert_sweep.sweep_template_alerts(
        _FakeSession()
    )
    assert out["opened"] == 0
    assert out["auto_resolved"] == 0
    assert state["creates"] == []
    assert state["resolves"] == []


async def test_hysteresis_band_with_open_ticket_keeps_it_open(monkeypatch):
    """7% failure rate (above 5% recovery threshold) + open ticket
    → leave the ticket alone. Only auto-resolve once metrics fully
    recover, not when they're merely elevated-but-not-critical."""
    existing = types.SimpleNamespace(
        id=999,
        patient_id="platform:template:dose_reminder_v2",
        category=delivery_template_alert_sweep.CATEGORY,
        sla_minutes=60,
    )
    state = _patch(
        monkeypatch,
        rows=[
            _row(
                template_name="dose_reminder_v2",
                total=50,
                failure_rate=0.07,
                failed=4,
            )
        ],
        open_tickets={"platform:template:dose_reminder_v2": existing},
    )

    out = await delivery_template_alert_sweep.sweep_template_alerts(
        _FakeSession()
    )
    assert out["auto_resolved"] == 0
    # Not above failure threshold → no "still elevated" re-note
    # either. Leave the timeline alone.
    assert state["resolves"] == []
    assert state["appends"] == []


# ---- Mixed pass --------------------------------------------------------


async def test_mixed_pass_independently_opens_resolves_and_skips(monkeypatch):
    """Real-world pass: one template recovering, one degrading,
    one quiet. Each must be evaluated independently and produce
    the right outcome — that's the whole reason this sweep walks
    rows instead of acting on the aggregate."""
    recovering = types.SimpleNamespace(
        id=10,
        patient_id="platform:template:appointment_reminder_v1",
        category=delivery_template_alert_sweep.CATEGORY,
        sla_minutes=60,
    )
    state = _patch(
        monkeypatch,
        rows=[
            # Healthy and recovering — was failing earlier, now at 2%.
            _row(
                template_name="appointment_reminder_v1",
                total=40,
                failure_rate=0.02,
                failed=1,
            ),
            # Newly degrading — never seen, now at 30% over enough volume.
            _row(
                template_name="dose_reminder_v2",
                total=20,
                failure_rate=0.30,
                failed=6,
            ),
            # Below volume floor — must be skipped, not flagged.
            _row(
                template_name="lab_closure_update_v1",
                total=2,
                failure_rate=1.0,
                failed=2,
            ),
        ],
        open_tickets={
            "platform:template:appointment_reminder_v1": recovering,
        },
    )

    out = await delivery_template_alert_sweep.sweep_template_alerts(
        _FakeSession()
    )
    assert out["templates_evaluated"] == 3
    assert out["opened"] == 1
    assert out["opened_templates"] == ["dose_reminder_v2"]
    assert out["auto_resolved"] == 1
    assert out["auto_resolved_templates"] == ["appointment_reminder_v1"]
    assert out["skipped_low_volume"] == 1
    # The newly-opened ticket has the right keying.
    assert state["creates"][0].patient_id == "platform:template:dose_reminder_v2"
    # The recovering ticket was resolved with id=10.
    assert state["resolves"][0][0] == 10


# ---- patient_id length-bound -------------------------------------------


def test_patient_id_truncates_to_column_width():
    """OpsTicket.patient_id is String(128). A pathologically long
    template name should NOT crash the create — truncate at 128
    chars defensively. Real template names are well under this
    limit; the cap is purely defensive."""
    long_name = "x" * 200
    pid = delivery_template_alert_sweep._patient_id_for_template(long_name)
    assert len(pid) <= 128
    assert pid.startswith("platform:template:")

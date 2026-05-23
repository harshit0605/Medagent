"""Unit tests for the inbound calendar-sync sweep.

The Google Calendar API call + the doctors_repo persistence are
mocked at the module's import boundary so this file covers the
RECONCILIATION logic — given a list of changes, do we mutate the
right appointments? Integration coverage with a real DB lives
in tests/integration/test_calendar_sync_sweep.py.

Reconciliation rules under test:
    1. Cancelled event → mark appointment cancelled (only if
       not already cancelled, idempotent).
    2. Rescheduled event (start/end change) → update
       scheduled_for + end_at on the appointment.
    3. Cancelled OR rescheduled to a state that already matches
       our DB → skip (no-op, no spurious mutation).
    4. Event we don't have an appointment for → skip (likely
       a doctor-created direct event, not ours).
    5. Per-doctor exceptions (OAuth expired, network error)
       don't kill the sweep for other doctors.
"""

from __future__ import annotations

import types
from datetime import datetime, timezone

from app.db.models import AppointmentStatus
from services.orchestrator import google_calendar as gcal
from services.scheduler import calendar_sync_sweep


def _appointment(
    *,
    id: int,
    doctor_id: int,
    event_id: str,
    scheduled_for: datetime,
    end_at: datetime,
    status: AppointmentStatus = AppointmentStatus.confirmed,
):
    return types.SimpleNamespace(
        id=id,
        doctor_id=doctor_id,
        calendar_event_id=event_id,
        scheduled_for=scheduled_for,
        end_at=end_at,
        status=status,
    )


class _StubResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return self._items


class _StubSession:
    """Async session stub. ``execute()`` looks up appointments by
    the IN clause; we precompute the answer."""

    def __init__(self, appointments_by_event_id: dict[str, object]):
        self._appts = appointments_by_event_id
        self.flushed = False

    async def execute(self, _stmt):
        return _StubResult(list(self._appts.values()))

    async def flush(self):
        self.flushed = True


def _patch_sync(
    monkeypatch,
    *,
    changes: list[gcal.CalendarChange],
    connected_doctors: list,
):
    """Stub the gcal incremental_sync + doctors_repo.list_connected
    so the sweep runs without real Google or DB calls."""

    async def fake_list_connected(_db):
        return connected_doctors

    async def fake_incremental_sync(_db, *, doctor_id):
        return gcal.IncrementalSyncResult(
            changes=changes, next_sync_token="tok-xyz"
        )

    monkeypatch.setattr(
        calendar_sync_sweep.doctors_repo,
        "list_connected",
        fake_list_connected,
    )
    monkeypatch.setattr(
        calendar_sync_sweep.gcal,
        "incremental_sync",
        fake_incremental_sync,
    )


# ---- Cancellation reconciliation ----------------------------------------


async def test_cancelled_event_marks_appointment_cancelled(monkeypatch):
    """Doctor deletes an event in Calendar → our matching
    appointment goes to ``cancelled``. The most common inbound
    sync case after a doctor moves an emergency to that slot."""
    appt = _appointment(
        id=1,
        doctor_id=10,
        event_id="event-A",
        scheduled_for=datetime(2026, 5, 8, 12, tzinfo=timezone.utc),
        end_at=datetime(2026, 5, 8, 12, 30, tzinfo=timezone.utc),
    )
    db = _StubSession({"event-A": appt})
    counters = await calendar_sync_sweep._reconcile_doctor.__wrapped__(
        # Direct call without the per-doctor try/except wrapper —
        # we want the raw counters, not the aggregated totals.
        db, doctor_id=10
    ) if hasattr(
        calendar_sync_sweep._reconcile_doctor, "__wrapped__"
    ) else None
    # _reconcile_doctor isn't wrapped; call directly.

    # Patch the gcal sync to return one cancellation.
    async def fake_sync(_db, *, doctor_id):
        return gcal.IncrementalSyncResult(
            changes=[gcal.CalendarChange(event_id="event-A", cancelled=True)],
            next_sync_token="tok",
        )

    monkeypatch.setattr(calendar_sync_sweep.gcal, "incremental_sync", fake_sync)

    counters = await calendar_sync_sweep._reconcile_doctor(
        db, doctor_id=10
    )

    assert counters["cancelled"] == 1
    assert counters["rescheduled"] == 0
    assert counters["skipped_unknown_event"] == 0
    assert appt.status == AppointmentStatus.cancelled


async def test_cancelled_event_already_cancelled_is_idempotent(monkeypatch):
    """Sweep re-runs over a cancellation we already applied →
    counted as ``skipped_already_correct``, not double-counted as
    a fresh cancellation. Without idempotency the sweep would
    keep "cancelling" the same already-cancelled row."""
    appt = _appointment(
        id=1,
        doctor_id=10,
        event_id="event-A",
        scheduled_for=datetime(2026, 5, 8, 12, tzinfo=timezone.utc),
        end_at=datetime(2026, 5, 8, 12, 30, tzinfo=timezone.utc),
        status=AppointmentStatus.cancelled,
    )
    db = _StubSession({"event-A": appt})

    async def fake_sync(_db, *, doctor_id):
        return gcal.IncrementalSyncResult(
            changes=[gcal.CalendarChange(event_id="event-A", cancelled=True)],
            next_sync_token="tok",
        )

    monkeypatch.setattr(calendar_sync_sweep.gcal, "incremental_sync", fake_sync)

    counters = await calendar_sync_sweep._reconcile_doctor(
        db, doctor_id=10
    )

    assert counters["cancelled"] == 0
    assert counters["skipped_already_correct"] == 1


# ---- Reschedule reconciliation ------------------------------------------


async def test_rescheduled_event_updates_appointment(monkeypatch):
    """Doctor drags an event to a new time in Calendar → our
    appointment's scheduled_for + end_at update accordingly."""
    appt = _appointment(
        id=1,
        doctor_id=10,
        event_id="event-A",
        scheduled_for=datetime(2026, 5, 8, 12, tzinfo=timezone.utc),
        end_at=datetime(2026, 5, 8, 12, 30, tzinfo=timezone.utc),
    )
    db = _StubSession({"event-A": appt})

    new_start = datetime(2026, 5, 8, 14, tzinfo=timezone.utc)
    new_end = datetime(2026, 5, 8, 14, 30, tzinfo=timezone.utc)

    async def fake_sync(_db, *, doctor_id):
        return gcal.IncrementalSyncResult(
            changes=[
                gcal.CalendarChange(
                    event_id="event-A",
                    cancelled=False,
                    summary="Patient X follow-up",
                    start=new_start,
                    end=new_end,
                )
            ],
            next_sync_token="tok",
        )

    monkeypatch.setattr(calendar_sync_sweep.gcal, "incremental_sync", fake_sync)

    counters = await calendar_sync_sweep._reconcile_doctor(
        db, doctor_id=10
    )

    assert counters["rescheduled"] == 1
    assert appt.scheduled_for == new_start
    assert appt.end_at == new_end


async def test_unchanged_event_is_idempotent(monkeypatch):
    """Sweep sees an event whose start/end already match our DB
    → no-op. Without this guard, every sweep would mark a 'reschedule'
    on every unchanged event."""
    appt = _appointment(
        id=1,
        doctor_id=10,
        event_id="event-A",
        scheduled_for=datetime(2026, 5, 8, 12, tzinfo=timezone.utc),
        end_at=datetime(2026, 5, 8, 12, 30, tzinfo=timezone.utc),
    )
    db = _StubSession({"event-A": appt})

    async def fake_sync(_db, *, doctor_id):
        return gcal.IncrementalSyncResult(
            changes=[
                gcal.CalendarChange(
                    event_id="event-A",
                    cancelled=False,
                    summary="Same event",
                    start=appt.scheduled_for,
                    end=appt.end_at,
                )
            ],
            next_sync_token="tok",
        )

    monkeypatch.setattr(calendar_sync_sweep.gcal, "incremental_sync", fake_sync)

    counters = await calendar_sync_sweep._reconcile_doctor(
        db, doctor_id=10
    )

    assert counters["rescheduled"] == 0
    assert counters["skipped_already_correct"] == 1


# ---- Unknown events ------------------------------------------------------


async def test_event_we_dont_have_is_skipped(monkeypatch):
    """An event from the doctor's calendar that ISN'T in our
    appointments table (i.e. the doctor created it directly)
    should be skipped — it's not our event to track."""
    db = _StubSession({})  # no appointments

    async def fake_sync(_db, *, doctor_id):
        return gcal.IncrementalSyncResult(
            changes=[
                gcal.CalendarChange(
                    event_id="event-NOT-OURS",
                    cancelled=False,
                    summary="Doctor-created direct event",
                    start=datetime(2026, 5, 8, 12, tzinfo=timezone.utc),
                    end=datetime(2026, 5, 8, 12, 30, tzinfo=timezone.utc),
                )
            ],
            next_sync_token="tok",
        )

    monkeypatch.setattr(calendar_sync_sweep.gcal, "incremental_sync", fake_sync)

    counters = await calendar_sync_sweep._reconcile_doctor(
        db, doctor_id=10
    )

    assert counters["skipped_unknown_event"] == 1
    assert counters["cancelled"] == 0
    assert counters["rescheduled"] == 0


# ---- Per-doctor failure isolation ---------------------------------------


async def test_per_doctor_failure_does_not_kill_sweep(monkeypatch):
    """One doctor's OAuth-expired or network failure must NOT
    abort the sweep for other doctors. The sweep iterates the
    full panel and tallies errors separately."""
    doctor_ok = types.SimpleNamespace(id=1)
    doctor_broken = types.SimpleNamespace(id=2)

    async def fake_list_connected(_db):
        return [doctor_ok, doctor_broken]

    call_log: list[int] = []

    async def fake_sync(_db, *, doctor_id):
        call_log.append(doctor_id)
        if doctor_id == 2:
            raise PermissionError("OAuth revoked")
        return gcal.IncrementalSyncResult(changes=[], next_sync_token="tok")

    monkeypatch.setattr(
        calendar_sync_sweep.doctors_repo,
        "list_connected",
        fake_list_connected,
    )
    monkeypatch.setattr(
        calendar_sync_sweep.gcal, "incremental_sync", fake_sync
    )

    out = await calendar_sync_sweep.sweep_calendar_changes(
        types.SimpleNamespace()
    )

    # Both doctors got a sync attempt.
    assert call_log == [1, 2]
    assert out["doctors_evaluated"] == 2
    assert out["totals"]["errors"] == 1
    # The OK doctor still completed cleanly.
    ok_entry = next(
        e for e in out["per_doctor"] if e["doctor_id"] == 1
    )
    assert "error" not in ok_entry
    # The broken doctor is annotated with the failure reason.
    broken_entry = next(
        e for e in out["per_doctor"] if e["doctor_id"] == 2
    )
    assert broken_entry.get("error") == "oauth_expired"


async def test_no_connected_doctors_returns_zero_evaluated(monkeypatch):
    async def fake_list_connected(_db):
        return []

    monkeypatch.setattr(
        calendar_sync_sweep.doctors_repo,
        "list_connected",
        fake_list_connected,
    )

    out = await calendar_sync_sweep.sweep_calendar_changes(
        types.SimpleNamespace()
    )
    assert out["doctors_evaluated"] == 0
    assert out["totals"]["cancelled"] == 0


# ---- Timezone normalisation -----------------------------------------------


async def test_naive_datetime_handled_as_utc(monkeypatch):
    """Postgres can return naive datetimes for the appointment
    columns. The reconciler must normalise both sides to UTC
    before comparing or every naive vs aware comparison would
    spuriously look like a reschedule."""
    naive_start = datetime(2026, 5, 8, 12)  # no tzinfo
    naive_end = datetime(2026, 5, 8, 12, 30)
    appt = _appointment(
        id=1,
        doctor_id=10,
        event_id="event-A",
        scheduled_for=naive_start,
        end_at=naive_end,
    )
    db = _StubSession({"event-A": appt})

    aware_same_start = naive_start.replace(tzinfo=timezone.utc)
    aware_same_end = naive_end.replace(tzinfo=timezone.utc)

    async def fake_sync(_db, *, doctor_id):
        return gcal.IncrementalSyncResult(
            changes=[
                gcal.CalendarChange(
                    event_id="event-A",
                    cancelled=False,
                    start=aware_same_start,
                    end=aware_same_end,
                )
            ],
            next_sync_token="tok",
        )

    monkeypatch.setattr(calendar_sync_sweep.gcal, "incremental_sync", fake_sync)

    counters = await calendar_sync_sweep._reconcile_doctor(
        db, doctor_id=10
    )

    # Should be idempotent — same wall time on both sides.
    assert counters["rescheduled"] == 0
    assert counters["skipped_already_correct"] == 1

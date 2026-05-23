"""Unit tests for the refill-reminder materializer (no DB)."""

from __future__ import annotations

import types
from datetime import date

from services.scheduler.refill_reminders import supply_runs_out


def _regimen(*, days=30, started=date(2026, 5, 1), tz="UTC"):
    return types.SimpleNamespace(
        id=1,
        patient_id=2,
        medication_name="Metformin",
        dose="500 mg",
        schedule={"timezone": tz, "times": ["08:00"], "type": "times_of_day"},
        starts_on=None,
        ends_on=None,
        supply_days_initial=days,
        supply_started_on=started,
    )


def test_supply_runs_out_returns_start_plus_days():
    r = _regimen(days=30, started=date(2026, 5, 1))
    assert supply_runs_out(r) == date(2026, 5, 31)


def test_supply_runs_out_none_when_unconfigured():
    r = _regimen()
    r.supply_days_initial = None
    assert supply_runs_out(r) is None
    r2 = _regimen()
    r2.supply_started_on = None
    assert supply_runs_out(r2) is None

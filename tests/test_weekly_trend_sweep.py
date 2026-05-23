"""Unit test for the weekly-trend summary helper (no DB)."""

from __future__ import annotations

from decimal import Decimal

from app.db.models import MetricObservation
from services.scheduler.weekly_trend_sweep import summarize_observations


def _obs(metric_key: str, value: str) -> MetricObservation:
    o = MetricObservation()
    o.metric_key = metric_key
    o.value = Decimal(value)
    o.source = "patient_self_report"
    return o


def test_summarize_groups_by_metric_latest_first():
    # newest-first input
    observations = [
        _obs("blood_glucose", "140"),  # latest glucose
        _obs("blood_glucose", "150"),
        _obs("weight_kg", "72"),
    ]
    summary = {s["metric_key"]: s for s in summarize_observations(observations)}
    assert summary["blood_glucose"]["count"] == 2
    assert summary["blood_glucose"]["latest"] == "140"
    assert summary["blood_glucose"]["label"] == "Blood glucose"
    assert summary["blood_glucose"]["unit"] == "mg/dL"
    assert summary["weight_kg"]["count"] == 1
    assert summary["weight_kg"]["latest"] == "72"


def test_summarize_empty():
    assert summarize_observations([]) == []

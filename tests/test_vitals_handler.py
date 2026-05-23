"""Unit tests for the vitals self-report parser (no DB).

The parser is the safety-critical, high-volume part — it must capture real
readings and reject stray numbers ("I take 2 tablets"). Deterministic regex,
so fully unit-testable.
"""

from __future__ import annotations

from decimal import Decimal

from services.orchestrator.vitals_handler import (
    looks_like_vitals_log,
    parse_vitals,
)


def _by_metric(text: str) -> dict[str, Decimal]:
    return {r.metric_key: r.value for r in parse_vitals(text)}


# ---- positive cases --------------------------------------------------------


def test_parse_blood_glucose_variants():
    for text in [
        "sugar 140",
        "my blood sugar is 140",
        "glucose 140",
        "sugar level 140",
        "fasting sugar 140",
        "bs 140",
    ]:
        readings = _by_metric(text)
        assert readings.get("blood_glucose") == Decimal("140"), text


def test_parse_blood_pressure_yields_two_readings():
    readings = _by_metric("BP 130/85")
    assert readings["bp_systolic"] == Decimal("130")
    assert readings["bp_diastolic"] == Decimal("85")
    # Also without the keyword, on a plausible pair.
    readings2 = _by_metric("blood pressure 120/80")
    assert readings2["bp_systolic"] == Decimal("120")
    assert readings2["bp_diastolic"] == Decimal("80")


def test_parse_weight_with_and_without_unit():
    assert _by_metric("weight 72")["weight_kg"] == Decimal("72")
    assert _by_metric("weight 72 kg")["weight_kg"] == Decimal("72")
    assert _by_metric("wt 72.5kg")["weight_kg"] == Decimal("72.5")


def test_parse_hba1c_decimal():
    assert _by_metric("hba1c 7.2")["hba1c"] == Decimal("7.2")
    assert _by_metric("a1c 6")["hba1c"] == Decimal("6")


def test_parse_peak_flow():
    assert _by_metric("peak flow 400")["peak_flow"] == Decimal("400")
    assert _by_metric("pef 350")["peak_flow"] == Decimal("350")


def test_parse_multiple_metrics_in_one_message():
    readings = _by_metric("sugar 140 and weight 72kg")
    assert readings["blood_glucose"] == Decimal("140")
    assert readings["weight_kg"] == Decimal("72")


# ---- negative / guard cases ------------------------------------------------


def test_rejects_non_vitals_numbers():
    for text in [
        "I take 2 tablets at 9pm",
        "call me at 3",
        "feeling dizzy today",
        "thanks doctor",
        "see you on 12/2026",  # date-like slash, diastolic out of range
        "ratio 1/2",  # systolic out of range
        "",
    ]:
        assert parse_vitals(text) == [], text
        assert looks_like_vitals_log(text) is False, text


def test_out_of_range_values_rejected():
    # Glucose 9000 (typo) — implausible, dropped.
    assert _by_metric("sugar 9000") == {}
    # Weight 5 kg — implausible for an adult patient, dropped.
    assert "weight_kg" not in _by_metric("weight 5")


def test_looks_like_vitals_log_true_for_real_reading():
    assert looks_like_vitals_log("sugar 140") is True
    assert looks_like_vitals_log("BP 130/85") is True

"""Unit tests for the sliding-scale insulin dosing module (pure, no I/O)."""

from __future__ import annotations

from services.orchestrator.insulin import (
    is_sliding_scale,
    resolve_units,
    validate_sliding_scale,
)


def _rule() -> dict:
    return {
        "kind": "sliding_scale",
        "unit": "units",
        "bands": [
            {"min": 0, "max": 149, "units": 0},
            {"min": 150, "max": 199, "units": 2},
            {"min": 200, "max": 249, "units": 4},
            {"min": 250, "max": 299, "units": 6},
            {"min": 300, "max": 9999, "units": 8},
        ],
        "low_glucose_threshold": 70,
        "high_glucose_escalate": 400,
    }


# ---- validation ------------------------------------------------------------


def test_valid_rule_passes():
    assert validate_sliding_scale(_rule()) is True


def test_is_sliding_scale_predicate():
    assert is_sliding_scale(_rule()) is True
    assert is_sliding_scale(None) is False
    assert is_sliding_scale({"kind": "fixed"}) is False


def test_invalid_rules_rejected():
    assert validate_sliding_scale(None) is False
    assert validate_sliding_scale({}) is False
    assert validate_sliding_scale({"kind": "sliding_scale", "bands": []}) is False
    # negative units
    assert (
        validate_sliding_scale(
            {"kind": "sliding_scale", "bands": [{"min": 0, "max": 10, "units": -1}]}
        )
        is False
    )
    # min > max
    assert (
        validate_sliding_scale(
            {"kind": "sliding_scale", "bands": [{"min": 50, "max": 10, "units": 1}]}
        )
        is False
    )
    # non-numeric threshold
    assert (
        validate_sliding_scale({**_rule(), "low_glucose_threshold": "low"}) is False
    )


# ---- resolution ------------------------------------------------------------


def test_in_range_bands_resolve_units():
    rule = _rule()
    assert resolve_units(rule, 120).units == 0
    assert resolve_units(rule, 175).units == 2
    assert resolve_units(rule, 220).units == 4
    assert resolve_units(rule, 280).units == 6


def test_normal_band_does_not_escalate():
    rec = resolve_units(_rule(), 175)
    assert rec.escalate is False
    assert rec.reason == "ok"


def test_hypo_does_not_dose_and_escalates():
    rec = resolve_units(_rule(), 55)
    assert rec.units is None
    assert rec.escalate is True
    assert rec.reason == "hypo"


def test_severe_hyper_doses_top_band_but_escalates():
    rec = resolve_units(_rule(), 420)
    assert rec.units == 8  # top band
    assert rec.escalate is True
    assert rec.reason == "severe_hyper"


def test_boundary_values_are_inclusive():
    rule = _rule()
    assert resolve_units(rule, 150).units == 2  # band min
    assert resolve_units(rule, 199).units == 2  # band max
    assert resolve_units(rule, 200).units == 4  # next band min

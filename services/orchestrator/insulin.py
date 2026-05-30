"""Sliding-scale insulin dosing — pure validation + resolution (no I/O).

SoT §3A: insulin timing + glucose-conditional dosing. A sliding-scale insulin
order maps a blood-glucose reading to a recommended number of units. This
module validates a ``dosing_rule`` (stored on ``Regimen.dosing_rule``) and
resolves a reading to a recommendation.

CRITICAL SAFETY STANCE: this NEVER auto-administers and NEVER tells a patient
to inject without a care-team-defined scale. It only echoes back the scale the
clinician already prescribed, and it ALWAYS escalates the two dangerous ends:

  * Hypoglycaemia (glucose < ``low_glucose_threshold``): do NOT suggest a dose;
    flag it — low sugar is an emergency, insulin would make it worse.
  * Severe hyperglycaemia (glucose >= ``high_glucose_escalate``): suggest the
    top band BUT flag for urgent human review (possible DKA).

The recommendation is advisory; the patient-facing copy always says "your care
team's scale suggests X — confirm with them", never "inject X".
"""

from __future__ import annotations

from dataclasses import dataclass

SLIDING_SCALE_KIND = "sliding_scale"


@dataclass(frozen=True)
class DoseRecommendation:
    """Outcome of resolving a glucose reading against a sliding scale."""

    units: int | None  # recommended units, or None when no dose should be given
    band_label: str  # human phrase describing the matched band / state
    escalate: bool  # True → flag for urgent human review
    reason: str  # short reason code: ok / hypo / severe_hyper / no_band


def validate_sliding_scale(rule: dict | None) -> bool:
    """True iff ``rule`` is a well-formed sliding-scale dosing rule.

    Required: ``kind == 'sliding_scale'`` and a non-empty ``bands`` list of
    ``{min, max, units}`` with numeric, non-negative, min<=max, units>=0.
    Optional ``low_glucose_threshold`` / ``high_glucose_escalate`` numeric.
    """
    if not isinstance(rule, dict):
        return False
    if rule.get("kind") != SLIDING_SCALE_KIND:
        return False
    bands = rule.get("bands")
    if not isinstance(bands, list) or not bands:
        return False
    for b in bands:
        if not isinstance(b, dict):
            return False
        try:
            lo = float(b["min"])
            hi = float(b["max"])
            units = int(b["units"])
        except (KeyError, TypeError, ValueError):
            return False
        if lo < 0 or hi < lo or units < 0:
            return False
    for opt in ("low_glucose_threshold", "high_glucose_escalate"):
        if opt in rule:
            try:
                float(rule[opt])
            except (TypeError, ValueError):
                return False
    return True


def is_sliding_scale(rule: dict | None) -> bool:
    """Cheap predicate — does this regimen carry a sliding-scale insulin rule?"""
    return isinstance(rule, dict) and rule.get("kind") == SLIDING_SCALE_KIND


def resolve_units(rule: dict, glucose_mg_dl: float) -> DoseRecommendation:
    """Resolve a glucose reading (mg/dL) to a dose recommendation.

    Assumes ``validate_sliding_scale(rule)`` is True (callers should guard).
    Safety: hypo → no dose + escalate; severe-hyper → top band + escalate.
    """
    low = rule.get("low_glucose_threshold")
    high = rule.get("high_glucose_escalate")

    # Hypoglycaemia — never dose insulin into a low reading.
    if low is not None and glucose_mg_dl < float(low):
        return DoseRecommendation(
            units=None,
            band_label=f"low blood sugar (under {int(float(low))} mg/dL)",
            escalate=True,
            reason="hypo",
        )

    # Find the band whose [min, max] contains the reading.
    matched = None
    for b in rule["bands"]:
        if float(b["min"]) <= glucose_mg_dl <= float(b["max"]):
            matched = b
            break

    if matched is None:
        # Reading above the top band's max (or a gap) — treat as severe.
        top = max(rule["bands"], key=lambda b: float(b["max"]))
        return DoseRecommendation(
            units=int(top["units"]),
            band_label="above the charted range",
            escalate=True,
            reason="no_band",
        )

    severe = high is not None and glucose_mg_dl >= float(high)
    return DoseRecommendation(
        units=int(matched["units"]),
        band_label=f"{int(float(matched['min']))}–{int(float(matched['max']))} mg/dL",
        escalate=severe,
        reason="severe_hyper" if severe else "ok",
    )

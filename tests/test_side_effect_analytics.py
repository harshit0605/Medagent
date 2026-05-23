"""Unit tests for the side-effect analytics extraction helpers.

The aggregation against real ops_tickets + regimens lives in
tests/integration/test_side_effect_analytics.py; this file
covers the pure-function helpers that don't need a DB.

The helpers' behaviour matters because they're the ATTRIBUTION
boundary — getting them wrong silently misroutes reports to
the wrong medications + symptoms.
"""

from __future__ import annotations

from services.orchestrator.side_effect_analytics import (
    _extract_medications,
    _extract_symptoms,
)


# ---- _extract_symptoms ---------------------------------------------------


def test_extracts_canonical_symptom_label():
    """A report saying "vomited" should bucket under the canonical
    ``vomiting`` label so two reports — one saying "vomited" and
    one saying "vomiting" — aggregate together."""
    assert _extract_symptoms("I vomited twice today") == ["vomiting"]


def test_extracts_multiple_distinct_symptoms():
    out = _extract_symptoms(
        "I'm feeling dizzy and have a bad headache, also some nausea"
    )
    assert "dizziness" in out
    assert "headache" in out
    assert "nausea" in out


def test_deduplicates_within_a_single_report():
    """A report mentioning the same symptom three times should
    contribute ONE label — the analytics roll-up wants
    "this report mentioned dizziness" as a binary signal."""
    out = _extract_symptoms(
        "dizzy in the morning, dizzy at lunch, dizziness at night"
    )
    assert out.count("dizziness") == 1


def test_word_boundary_prevents_substring_false_positives():
    """``rash`` shouldn't match ``harassment`` or ``rather``."""
    out = _extract_symptoms("I'm rather harassed by these")
    assert "rash" not in out


def test_handles_empty_or_none():
    assert _extract_symptoms("") == []
    assert _extract_symptoms(None) == []


def test_case_insensitive():
    out = _extract_symptoms("DIZZY today")
    assert out == ["dizziness"]


# ---- _extract_medications ------------------------------------------------


def test_attributes_only_to_active_regimens():
    """A patient on metformin who reports "metformin causes
    headaches" attributes to metformin. A patient who reports
    the same thing but is on atorvastatin (NOT metformin)
    attributes to nothing — strict attribution prevents
    misrouting reports to drugs the patient isn't on."""
    text = "metformin gave me headaches"
    on_metformin = _extract_medications(
        text, regimen_meds=["Metformin"]
    )
    on_atorva = _extract_medications(
        text, regimen_meds=["Atorvastatin"]
    )
    assert on_metformin == ["Metformin"]
    assert on_atorva == []


def test_case_insensitive_match():
    out = _extract_medications(
        "METFORMIN is making me sick", regimen_meds=["metformin"]
    )
    assert out == ["metformin"]


def test_word_boundary_prevents_partial_matches():
    """``vita`` shouldn't match ``vitamin``. The regimen-name
    cross-reference must require full-word match."""
    out = _extract_medications(
        "vita seems fine but I have nausea",
        regimen_meds=["vitamin D"],
    )
    assert out == []


def test_multiple_active_regimens_only_mentioned_ones_attributed():
    out = _extract_medications(
        "the atorvastatin is fine but metformin gives me nausea",
        regimen_meds=["Metformin", "Atorvastatin", "Lisinopril"],
    )
    # Both mentioned → both attributed; lisinopril not mentioned.
    assert "Metformin" in out
    assert "Atorvastatin" in out
    assert "Lisinopril" not in out


def test_no_regimens_returns_empty():
    """Patient with no active regimens → no attribution possible
    even if the text mentions a drug name."""
    out = _extract_medications(
        "metformin caused this", regimen_meds=[]
    )
    assert out == []


def test_handles_empty_text_or_none():
    assert (
        _extract_medications("", regimen_meds=["Metformin"]) == []
    )
    assert (
        _extract_medications(None, regimen_meds=["Metformin"]) == []
    )

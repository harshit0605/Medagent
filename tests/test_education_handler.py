"""Unit tests for the educational microcontent handler (G5)."""

from __future__ import annotations

import pytest

from services.orchestrator.education_handler import (
    handle_education_query,
    looks_like_education_query,
    match_snippet,
)


def test_matches_known_topics():
    assert match_snippet("what is hba1c?").topic == "hba1c"
    assert match_snippet("how do I use a spacer").topic == "spacer"
    assert match_snippet("what's a normal bp?").topic == "blood_pressure"
    assert match_snippet("how should I store insulin").topic == "insulin_storage"


def test_longest_trigger_wins():
    # "use a spacer" should match the spacer topic, not a generic.
    assert match_snippet("can you tell me how to use a spacer").topic == "spacer"


def test_no_match_returns_none():
    assert match_snippet("sugar 140") is None
    assert match_snippet("I have chest pain") is None
    assert match_snippet("") is None
    assert match_snippet(None) is None


def test_missed_dose_education_is_question_only():
    # A QUESTION about missing doses → education.
    assert match_snippet("what if I miss a dose?").topic == "missed_dose"
    # A STATEMENT of missing a dose → NOT education (routes to adherence).
    assert match_snippet("I missed a dose this morning") is None


def test_gate_predicate():
    assert looks_like_education_query("what is hba1c") is True
    assert looks_like_education_query("hello there") is False


@pytest.mark.asyncio
async def test_handle_returns_content_with_disclaimer():
    delta = await handle_education_query(
        patient_phone="+9199", new_user_text="what is hba1c?"
    )
    assert delta is not None
    assert "blood sugar" in delta["response_body"].lower()
    assert "not personal medical advice" in delta["response_body"].lower()
    assert delta["audit_reasons"] == ["education_hba1c"]


@pytest.mark.asyncio
async def test_handle_no_match_returns_none():
    delta = await handle_education_query(
        patient_phone="+9199", new_user_text="random chatter"
    )
    assert delta is None

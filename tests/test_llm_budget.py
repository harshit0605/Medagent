"""Per-patient LLM token-budget gate (C2).

The budget short-circuits LLM calls to the deterministic fallback once a
patient has burned through their daily token allowance, without erroring.
No real OpenAI / DB — the token-sum query is monkeypatched.
"""

from __future__ import annotations

import pytest

from services.orchestrator.llm import AgentLLM


def _llm() -> AgentLLM:
    # api_key present so .enabled would be True; we only test _budget_ok.
    return AgentLLM(api_key="sk-test", enabled=True)


@pytest.mark.asyncio
async def test_budget_disabled_allows(monkeypatch):
    monkeypatch.delenv("LLM_PATIENT_DAILY_TOKEN_BUDGET", raising=False)
    assert await _llm()._budget_ok() is True


@pytest.mark.asyncio
async def test_no_patient_context_allows(monkeypatch):
    monkeypatch.setenv("LLM_PATIENT_DAILY_TOKEN_BUDGET", "1000")
    # Default contextvar has session=None / patient_id=None → platform call.
    from services.orchestrator import llm_tracking

    llm_tracking.set_llm_tracking_context()  # clear
    assert await _llm()._budget_ok() is True


@pytest.mark.asyncio
async def test_under_budget_allows(monkeypatch):
    monkeypatch.setenv("LLM_PATIENT_DAILY_TOKEN_BUDGET", "1000")
    from services.orchestrator import llm_tracking

    object_session = object()
    llm_tracking.set_llm_tracking_context(
        session=object_session, patient_id="+9199"
    )

    async def fake_sum(_session, *, patient_id, since):
        return 500  # under 1000

    monkeypatch.setattr(llm_tracking, "patient_tokens_since", fake_sum)
    assert await _llm()._budget_ok() is True


@pytest.mark.asyncio
async def test_over_budget_blocks(monkeypatch):
    monkeypatch.setenv("LLM_PATIENT_DAILY_TOKEN_BUDGET", "1000")
    from services.orchestrator import llm_tracking

    llm_tracking.set_llm_tracking_context(
        session=object(), patient_id="+9199"
    )

    async def fake_sum(_session, *, patient_id, since):
        return 1500  # over 1000

    monkeypatch.setattr(llm_tracking, "patient_tokens_since", fake_sum)
    assert await _llm()._budget_ok() is False


@pytest.mark.asyncio
async def test_query_failure_fails_open(monkeypatch):
    """A budget-query error must not block the LLM — fail open (allow)."""
    monkeypatch.setenv("LLM_PATIENT_DAILY_TOKEN_BUDGET", "1000")
    from services.orchestrator import llm_tracking

    llm_tracking.set_llm_tracking_context(
        session=object(), patient_id="+9199"
    )

    async def boom(_session, *, patient_id, since):
        raise RuntimeError("db down")

    monkeypatch.setattr(llm_tracking, "patient_tokens_since", boom)
    assert await _llm()._budget_ok() is True

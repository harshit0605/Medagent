"""Integration tests against a real OpenAI deployment.

Skipped automatically when OPENAI_API_KEY is unset so the rest of the suite
remains hermetic. These tests assert the LLM-augmented graph produces the
right *kind* of output on a few representative inputs — they don't pin exact
strings, since the model output is non-deterministic.
"""

from __future__ import annotations

import os

import pytest

from services.orchestrator import agent_workflow, llm as llm_module


pytestmark = pytest.mark.skipif(
    not os.getenv("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set — skipping live LLM integration tests",
)


@pytest.fixture(autouse=True)
def _enable_real_llm():
    # conftest.py forces LLM_ENABLED=0 by default; flip it on for this module
    # only, and reset the cached singleton so the new env value is read.
    prior = os.environ.get("LLM_ENABLED")
    os.environ["LLM_ENABLED"] = "1"
    llm_module.reset_llm_for_tests()
    yield
    if prior is None:
        os.environ.pop("LLM_ENABLED", None)
    else:
        os.environ["LLM_ENABLED"] = prior
    llm_module.reset_llm_for_tests()


async def _run(text: str):
    from datetime import datetime, timedelta, timezone

    return await agent_workflow.run_agent_workflow(
        message_id="itest",
        patient_id="itest-patient",
        text=text,
        phone=None,
        last_user_message_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )


async def test_llm_classifies_paraphrased_refill_intent():
    result = await _run("hey, my pills are running low, can you arrange more?")
    assert result.intent == "refill_request"


async def test_llm_keeps_critical_floor_for_red_flag():
    result = await _run("I'm having chest pain and can't breathe")
    assert result.risk_level == "critical"
    assert result.escalation_required is True


async def test_llm_compose_returns_non_canned_body():
    result = await _run("I took my morning metformin without issues")
    # The canned adherence_update response includes FORGOT/SIDE_EFFECT/.../OTHER.
    # An LLM body shouldn't echo that fixed action list.
    assert "FORGOT, SIDE_EFFECT" not in result.response_body
    assert len(result.response_body) <= 600

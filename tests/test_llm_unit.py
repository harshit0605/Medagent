"""Unit tests for the LLM-augmented agent workflow nodes (async).

These tests do NOT hit OpenAI. They monkeypatch the AgentLLM singleton with
canned async responses and assert the workflow respects the fallback contract.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from services.orchestrator import agent_workflow, llm as llm_module


@pytest.fixture(autouse=True)
def _restore_singleton():
    yield
    llm_module.reset_llm_for_tests()


class _StubLLM:
    """Minimal stand-in for AgentLLM with hard-coded async behaviours."""

    def __init__(
        self,
        *,
        intent: str | None = None,
        safety: tuple[str, str] | None = None,
        compose: str | None = None,
        enabled: bool = True,
    ) -> None:
        self._intent = intent
        self._safety = safety
        self._compose = compose
        self.enabled = enabled

    async def classify_intent(self, text: str) -> str | None:
        return self._intent

    async def augment_safety(
        self, text: str, intent: str, current_severity: str
    ) -> tuple[str, str] | None:
        if self._safety is None:
            return None
        rank = {"low": 0, "medium": 1, "high": 2, "critical": 3}
        if rank[self._safety[0]] <= rank[current_severity]:
            return current_severity, ""
        return self._safety

    async def compose_reply(self, **_kwargs: Any) -> str | None:
        return self._compose


def _install(stub: _StubLLM) -> None:
    llm_module._llm_singleton = stub  # type: ignore[assignment]


async def test_llm_intent_overrides_keyword_rules():
    _install(_StubLLM(intent="symptom_report"))
    result = await agent_workflow.run_agent_workflow(
        message_id="m",
        patient_id="p",
        text="I felt off after my dose this morning",
        phone=None,
        last_user_message_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    assert result.intent == "symptom_report"


async def test_llm_intent_failure_falls_back_to_rules():
    _install(_StubLLM(intent=None))
    result = await agent_workflow.run_agent_workflow(
        message_id="m",
        patient_id="p",
        text="Need refill, running out",
        phone=None,
        last_user_message_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    assert result.intent == "refill_request"


async def test_safety_llm_can_only_escalate():
    _install(_StubLLM(intent="general_question", safety=("low", "noop")))
    result = await agent_workflow.run_agent_workflow(
        message_id="m",
        patient_id="p",
        text="thanks!",
        phone=None,
        last_user_message_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    assert result.risk_level == "low"
    assert result.escalation_required is False


async def test_safety_llm_escalates_above_rules_floor():
    _install(_StubLLM(intent="general_question", safety=("high", "subtle_breathlessness")))
    result = await agent_workflow.run_agent_workflow(
        message_id="m",
        patient_id="p",
        text="i feel a bit tight in my chest but maybe ok",
        phone=None,
        last_user_message_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    assert result.risk_level == "high"
    assert result.escalation_required is True
    assert result.escalation_reason == "subtle_breathlessness"


async def test_safety_llm_cannot_reduce_below_rules_floor():
    _install(_StubLLM(intent="symptom_report", safety=("medium", "softer")))
    result = await agent_workflow.run_agent_workflow(
        message_id="m",
        patient_id="p",
        text="I have chest pain",
        phone=None,
        last_user_message_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    assert result.risk_level == "critical"
    assert result.escalation_required is True
    assert result.escalation_reason == "critical_red_flag"


async def test_llm_compose_replaces_canned_body():
    _install(
        _StubLLM(
            intent="adherence_update",
            compose="Got it — glad you took your dose. Let me know if you need anything else.",
        )
    )
    result = await agent_workflow.run_agent_workflow(
        message_id="m",
        patient_id="p",
        text="just took my pill",
        phone=None,
        last_user_message_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    assert "Got it" in result.response_body
    assert "FORGOT, SIDE_EFFECT" not in result.response_body


async def test_llm_compose_failure_falls_back_to_canned():
    _install(_StubLLM(intent="adherence_update", compose=None))
    result = await agent_workflow.run_agent_workflow(
        message_id="m",
        patient_id="p",
        text="took it",
        phone=None,
        last_user_message_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    assert "FORGOT, SIDE_EFFECT" in result.response_body


async def test_disabled_llm_means_pure_rules():
    _install(_StubLLM(intent=None, safety=None, compose=None, enabled=False))
    result = await agent_workflow.run_agent_workflow(
        message_id="m",
        patient_id="p",
        text="I have chest pain",
        phone=None,
        last_user_message_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    assert result.intent == "symptom_report"
    assert result.risk_level == "critical"

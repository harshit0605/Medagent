"""Unit tests for the LLM cost-computation helper + tracker.

The persistence + DB-aware behaviour lives in
tests/integration/test_llm_tracking.py. This file covers the
pure-function arithmetic (cost calc) + the contextvar wiring
that doesn't need a DB.
"""

from __future__ import annotations

from services.orchestrator.llm_tracking import (
    LlmTrackingContext,
    compute_cost_micros,
    get_llm_tracking_context,
    set_llm_tracking_context,
)


# ---- compute_cost_micros -------------------------------------------------


def test_known_model_computes_nonzero_cost():
    """gpt-4o at 1k prompt + 500 completion: should produce a
    nonzero cost. The exact figure: 1000×2_500_000//1M +
    500×10_000_000//1M = 2500 + 5000 = 7500 micros (= $0.0075)."""
    cost = compute_cost_micros(
        model="gpt-4o", prompt_tokens=1000, completion_tokens=500
    )
    assert cost == 7500


def test_unknown_model_returns_zero():
    """Unknown models return 0 — tokens still get tracked, cost
    line shows $0.00 as a 'rate not configured' visible signal."""
    cost = compute_cost_micros(
        model="future-model-xyz",
        prompt_tokens=10000,
        completion_tokens=5000,
    )
    assert cost == 0


def test_zero_tokens_returns_zero():
    cost = compute_cost_micros(
        model="gpt-4o-mini", prompt_tokens=0, completion_tokens=0
    )
    assert cost == 0


def test_cheap_model_preserves_cents_via_multiply_first():
    """gpt-4o-mini is $0.60/M completion = 0.6 micros per token.
    Naive per-token rounding would zero out the cost; the
    multiply-tokens-first dance preserves it.

    1000 completion tokens × 600_000 micros/M ÷ 1M = 600 micros.
    """
    cost = compute_cost_micros(
        model="gpt-4o-mini", prompt_tokens=0, completion_tokens=1000
    )
    assert cost == 600


def test_prompt_and_completion_priced_separately():
    """Prompt + completion have different rates (completion is
    typically 4× more). The cost calc must use the per-side
    rates, not a single total-token rate."""
    prompt_only = compute_cost_micros(
        model="gpt-4o", prompt_tokens=10000, completion_tokens=0
    )
    completion_only = compute_cost_micros(
        model="gpt-4o", prompt_tokens=0, completion_tokens=10000
    )
    # gpt-4o: $2.50/M prompt, $10/M completion → completion 4× cost.
    assert completion_only == prompt_only * 4


# ---- Contextvar wiring --------------------------------------------------


def test_default_context_is_empty():
    """Pristine contextvar has all-None values so callers without
    request entry see no-op tracking, not an exception."""
    # ContextVars persist across tests in the same process
    # within the same context. Reset explicitly.
    set_llm_tracking_context(
        session=None, patient_id=None, message_id=None
    )
    ctx = get_llm_tracking_context()
    assert ctx.session is None
    assert ctx.patient_id is None
    assert ctx.message_id is None


def test_set_context_round_trips():
    """A request entry sets the context; downstream callers read
    the same values — that's the whole point of the propagation."""
    set_llm_tracking_context(
        session=None,  # session not testable without DB; covered by integration
        patient_id="phone-123",
        message_id="msg-abc",
    )
    ctx = get_llm_tracking_context()
    assert ctx.patient_id == "phone-123"
    assert ctx.message_id == "msg-abc"


def test_context_overrides_replace_not_merge():
    """A second ``set_llm_tracking_context`` call REPLACES the
    context — not merges. Otherwise consecutive requests would
    leak state into each other."""
    set_llm_tracking_context(
        session=None, patient_id="phone-A", message_id="msg-A"
    )
    set_llm_tracking_context(
        session=None, patient_id="phone-B", message_id=None
    )
    ctx = get_llm_tracking_context()
    assert ctx.patient_id == "phone-B"
    assert ctx.message_id is None  # cleared, not retained


def test_context_dataclass_is_a_clean_default():
    """The default-factory ContextVar yields a brand-new
    ``LlmTrackingContext`` with all fields ``None``. A misuse
    that mutates the default would corrupt every caller."""
    ctx = LlmTrackingContext()
    assert ctx.session is None
    assert ctx.patient_id is None
    assert ctx.message_id is None

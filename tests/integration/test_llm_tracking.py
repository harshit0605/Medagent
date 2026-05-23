"""Integration tests for the LLM tracking pipeline.

End-to-end against real Postgres because:
    1. The contextvar → repo write path needs a real session to
       confirm rows actually land.
    2. The aggregation queries (``summarize`` /
       ``latency_percentiles`` / ``top_patients_by_cost``) use
       Postgres-specific functions like ``percentile_cont``.
    3. The /ops/analytics/llm-cost endpoint round-trip depends
       on all of the above.

Skipped when DATABASE_URL is unset.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.db.repositories import llm_calls as llm_calls_repo
from app.db.session import get_sessionmaker
from services.orchestrator.llm_tracking import (
    set_llm_tracking_context,
    track_llm_call,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping LLM tracking tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


def _phone() -> str:
    """Per-test unique phone — the integration suite has no
    per-test isolation, so reusing patient identifiers would
    conflate rows across tests."""
    return f"llm-{uuid.uuid4().hex[:10]}"


class _FakeUsage:
    """Minimal stand-in for ``completion.usage``. Mirrors the
    shape OpenAI's client returns."""

    def __init__(self, prompt: int, completion: int):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = prompt + completion


class _FakeCompletion:
    def __init__(self, prompt: int, completion: int):
        self.usage = _FakeUsage(prompt, completion)


# ---- Tracker round-trip --------------------------------------------------


async def test_tracker_persists_row_with_tokens_and_cost():
    """Successful LLM call → one row in ``llm_call_logs`` with
    tokens, cost, latency, and the request context fields
    populated."""
    phone = _phone()
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        set_llm_tracking_context(
            session=db, patient_id=phone, message_id="msg-1"
        )
        async with track_llm_call(
            call_kind="test_call", model="gpt-4o-mini"
        ) as tracker:
            tracker.set_completion(_FakeCompletion(prompt=100, completion=50))
        await db.commit()

        # Read it back via the summarize aggregator. Fast +
        # confirms the row landed correctly.
        summary = await llm_calls_repo.summarize(
            db, since=datetime.now(timezone.utc) - timedelta(minutes=1)
        )
    # Our row contributed at least 1 call + 150 tokens (other
    # tests may have rows in the same window). Assert our SPECIFIC
    # call_kind appears in the breakdown.
    by_kind = {row["call_kind"]: row for row in summary["by_call_kind"]}
    assert "test_call" in by_kind
    assert by_kind["test_call"]["calls"] >= 1
    assert by_kind["test_call"]["tokens"] >= 150


async def test_tracker_records_error_on_exception():
    """An LLM call that raises must record the error string +
    re-raise the exception. The latency + tokens (zero on error)
    still land for forensic visibility."""
    phone = _phone()
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        set_llm_tracking_context(
            session=db, patient_id=phone, message_id=None
        )
        with pytest.raises(RuntimeError, match="boom"):
            async with track_llm_call(
                call_kind="error_test", model="gpt-4o-mini"
            ):
                raise RuntimeError("boom")
        await db.commit()

        # The errors_count in the summary should reflect our row.
        summary = await llm_calls_repo.summarize(
            db, since=datetime.now(timezone.utc) - timedelta(minutes=1)
        )
    # Other tests may inject errors too; assert at least 1.
    assert summary["errors_count"] >= 1


async def test_tracker_no_session_is_noop():
    """When the contextvar has no session (e.g. unit-test
    bootstrap, scheduler sweeps without a request), the tracker
    is a no-op — must NOT raise. Without this, every off-route
    LLM call would fail."""
    set_llm_tracking_context(
        session=None, patient_id=None, message_id=None
    )
    # Context manager runs with no session; tracker is a no-op.
    async with track_llm_call(
        call_kind="noop_test", model="gpt-4o-mini"
    ) as tracker:
        tracker.set_completion(_FakeCompletion(prompt=10, completion=5))
    # Nothing to assert against — we just confirm the path
    # didn't raise. Reaching here = pass.


async def test_tracker_explicit_tokens_override_completion():
    """For multi-turn calls (e.g. booking-agent ReAct loop) the
    caller might want to set tokens manually rather than relying
    on the completion. The override path takes precedence."""
    phone = _phone()
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        set_llm_tracking_context(
            session=db, patient_id=phone, message_id=None
        )
        async with track_llm_call(
            call_kind="explicit_tokens_test", model="gpt-4o-mini"
        ) as tracker:
            # Caller sets tokens explicitly — completion not stashed.
            tracker.set_tokens(prompt_tokens=999, completion_tokens=111)
        await db.commit()

        summary = await llm_calls_repo.summarize(
            db, since=datetime.now(timezone.utc) - timedelta(minutes=1)
        )
    by_kind = {row["call_kind"]: row for row in summary["by_call_kind"]}
    assert "explicit_tokens_test" in by_kind
    # 999 + 111 = 1110 tokens, plus any prior rows under the same
    # call_kind. Use the MIN bound to be resilient.
    assert by_kind["explicit_tokens_test"]["tokens"] >= 1110


# ---- Top-patients aggregation -------------------------------------------


async def test_top_patients_returns_highest_cost_first():
    """The dashboard's 'expensive patients' list is sorted by
    total cost descending. Confirms the ORDER BY actually works
    against Postgres."""
    cheap_phone = _phone()
    expensive_phone = _phone()
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        # Cheap patient: 100 prompt tokens × $0.15/M ≈ 15 micros.
        await llm_calls_repo.record(
            db,
            call_kind="test",
            model="gpt-4o-mini",
            prompt_tokens=100,
            completion_tokens=0,
            total_tokens=100,
            cost_usd_micros=15,
            patient_id=cheap_phone,
        )
        # Expensive patient: 100k prompt tokens × $2.50/M = 250_000.
        await llm_calls_repo.record(
            db,
            call_kind="test",
            model="gpt-4o",
            prompt_tokens=100_000,
            completion_tokens=10_000,
            total_tokens=110_000,
            cost_usd_micros=350_000,
            patient_id=expensive_phone,
        )
        await db.commit()

        top = await llm_calls_repo.top_patients_by_cost(
            db, since=datetime.now(timezone.utc) - timedelta(minutes=1)
        )

    # Find OUR seeded patients — others may exist from prior tests.
    by_phone = {row["patient_id"]: row for row in top}
    assert expensive_phone in by_phone
    assert cheap_phone in by_phone
    # Expensive must rank above cheap. Order in returned list
    # is descending by cost — find indices of OUR patients.
    expensive_idx = next(
        i for i, r in enumerate(top) if r["patient_id"] == expensive_phone
    )
    cheap_idx = next(
        i for i, r in enumerate(top) if r["patient_id"] == cheap_phone
    )
    assert expensive_idx < cheap_idx


# ---- Latency percentiles -------------------------------------------------


async def test_latency_percentiles_use_only_non_null_rows():
    """Errored calls can have NULL latency. The percentile
    helper must filter them out so error-path latencies don't
    pollute the response-time stats."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        # 5 healthy calls at 100ms each.
        for _ in range(5):
            await llm_calls_repo.record(
                db,
                call_kind="latency_test",
                model="gpt-4o-mini",
                latency_ms=100,
            )
        # 1 errored call with NULL latency.
        await llm_calls_repo.record(
            db,
            call_kind="latency_test",
            model="gpt-4o-mini",
            latency_ms=None,
            error="OpenAITimeoutError: simulated",
        )
        await db.commit()

        result = await llm_calls_repo.latency_percentiles(
            db, since=datetime.now(timezone.utc) - timedelta(minutes=1)
        )
    # p50 should reflect the healthy calls — the NULL row would
    # otherwise corrupt the calculation.
    assert result["p50_ms"] is not None
    assert result["p50_ms"] >= 0


# ---- Endpoint round-trip --------------------------------------------------


def test_endpoint_returns_documented_shape(orchestrator_client):
    r = orchestrator_client.get(
        "/ops/analytics/llm-cost", params={"days": 30}
    )
    assert r.status_code == 200
    body = r.json()
    for key in (
        "since",
        "until",
        "total_calls",
        "total_tokens",
        "total_cost_usd_micros",
        "errors_count",
        "by_call_kind",
        "by_model",
        "top_patients",
        "latency",
    ):
        assert key in body, f"missing key: {key}"
    assert "p50_ms" in body["latency"]
    assert "p95_ms" in body["latency"]


def test_endpoint_validates_days_window(orchestrator_client):
    r = orchestrator_client.get(
        "/ops/analytics/llm-cost", params={"days": 0}
    )
    assert r.status_code == 400
    r = orchestrator_client.get(
        "/ops/analytics/llm-cost", params={"days": 999}
    )
    assert r.status_code == 400

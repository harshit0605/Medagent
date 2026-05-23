"""Unit tests for the inbound rate-limit counter.

Exercises the gate's pure-decision logic against a stubbed session.
The DB-side count query is mocked so we test the threshold + return
shape independently of Postgres. Integration coverage with a real
DB lives in tests/integration/test_rate_limiter.py.
"""

from __future__ import annotations

from datetime import datetime, timezone

from services.orchestrator import rate_limiter


class _StubResult:
    """Stand-in for the tuple a SELECT count() returns."""

    def __init__(self, value: int) -> None:
        self._value = value

    def scalar(self) -> int:
        return self._value


class _StubSession:
    """Async session stub that returns a configurable scalar count."""

    def __init__(self, count: int) -> None:
        self._count = count
        self.executed: list[object] = []

    async def execute(self, stmt) -> _StubResult:
        self.executed.append(stmt)
        return _StubResult(self._count)


# ---- Defensive empty-phone branch ----------------------------------------


async def test_empty_phone_short_circuits_without_db_call():
    """Missing phone → return is_limited=False without touching the
    DB. A malformed inbound shouldn't accidentally trip the gate
    or burn an extra round-trip."""
    db = _StubSession(count=999)
    result = await rate_limiter.check_inbound_rate_limit(
        db, patient_phone=""
    )
    assert result.is_limited is False
    assert result.count == 0
    # No DB query made.
    assert db.executed == []


async def test_none_phone_short_circuits_without_db_call():
    db = _StubSession(count=999)
    result = await rate_limiter.check_inbound_rate_limit(
        db, patient_phone=None  # type: ignore[arg-type]
    )
    assert result.is_limited is False
    assert db.executed == []


# ---- Below threshold -----------------------------------------------------


async def test_below_threshold_not_limited():
    db = _StubSession(count=5)  # well under default 30
    result = await rate_limiter.check_inbound_rate_limit(
        db, patient_phone="9100"
    )
    assert result.is_limited is False
    assert result.count == 5
    assert result.limit == 30
    assert result.window_minutes == 5


# ---- At / above threshold ------------------------------------------------


async def test_at_threshold_is_limited():
    """Threshold is inclusive — count == limit fires the gate.
    A loop hitting exactly 30/window shouldn't be the
    edge case that slips through."""
    db = _StubSession(count=30)
    result = await rate_limiter.check_inbound_rate_limit(
        db, patient_phone="9100"
    )
    assert result.is_limited is True
    assert result.count == 30


async def test_well_above_threshold_is_limited():
    db = _StubSession(count=500)
    result = await rate_limiter.check_inbound_rate_limit(
        db, patient_phone="9100"
    )
    assert result.is_limited is True
    assert result.count == 500


# ---- Env override --------------------------------------------------------


async def test_env_override_changes_threshold(monkeypatch):
    """The threshold + window are env-configurable so the policy
    can be tuned per-deploy. A tighter threshold via env should
    change the gate's behaviour at the same count."""
    monkeypatch.setenv("INBOUND_RATE_LIMIT_COUNT", "10")
    monkeypatch.setenv("INBOUND_RATE_LIMIT_WINDOW_MINUTES", "15")

    db = _StubSession(count=10)
    result = await rate_limiter.check_inbound_rate_limit(
        db, patient_phone="9100"
    )
    assert result.is_limited is True
    assert result.limit == 10
    assert result.window_minutes == 15


async def test_invalid_env_falls_back_to_default(monkeypatch):
    """Bogus env value (typo, accidental string) must NOT crash —
    fall back to the documented default. Production deploys
    might have garbage values from misconfigured stacks."""
    monkeypatch.setenv("INBOUND_RATE_LIMIT_COUNT", "not-a-number")

    db = _StubSession(count=29)  # one below the 30 default
    result = await rate_limiter.check_inbound_rate_limit(
        db, patient_phone="9100"
    )
    assert result.is_limited is False
    assert result.limit == 30


# ---- now=... parameter ---------------------------------------------------


async def test_explicit_now_passed_to_query():
    """The ``now`` parameter is what the deterministic-test
    integration test pins to. A stable ``now`` makes the cutoff
    computation reproducible."""
    fixed = datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)
    db = _StubSession(count=0)
    result = await rate_limiter.check_inbound_rate_limit(
        db, patient_phone="9100", now=fixed
    )
    # Just confirm the call works with explicit now; the cutoff
    # calculation is implicit via timedelta(minutes=window).
    assert result.is_limited is False
    assert len(db.executed) == 1

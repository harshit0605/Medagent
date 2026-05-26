"""Unit tests for the in-process circuit breaker primitive.

No clock dependency — uses ``time.monotonic`` patching to deterministically
advance past the reset window.
"""

from __future__ import annotations

from unittest.mock import patch

from app.circuit_breaker import CircuitBreaker


def test_initial_state_is_closed():
    cb = CircuitBreaker(name="x", threshold=3)
    assert cb.is_open() is False
    assert cb.snapshot()["consecutive_failures"] == 0


def test_failures_below_threshold_keep_closed():
    cb = CircuitBreaker(name="x", threshold=3)
    cb.record_failure()
    cb.record_failure()
    assert cb.is_open() is False


def test_threshold_failures_open_the_breaker():
    cb = CircuitBreaker(name="x", threshold=3)
    cb.record_failure()
    cb.record_failure()
    cb.record_failure()
    assert cb.is_open() is True
    snap = cb.snapshot()
    assert snap["consecutive_failures"] == 3
    assert snap["open"] is True


def test_success_resets_counter_even_below_threshold():
    cb = CircuitBreaker(name="x", threshold=3)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    cb.record_failure()
    cb.record_failure()
    # Two new fails after reset — still below threshold.
    assert cb.is_open() is False


def test_reset_window_transitions_to_half_open_then_closed_on_success():
    """After the reset window elapses, the next is_open() returns False
    (half-open probe). A success there closes the breaker properly."""
    cb = CircuitBreaker(name="x", threshold=2, reset_after_seconds=10.0)
    cb.record_failure()
    cb.record_failure()
    assert cb.is_open() is True

    base = 1000.0
    with patch("app.circuit_breaker.time.monotonic", return_value=base):
        # Set the open deadline to base + 10s explicitly by re-triggering.
        cb.reset()
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open() is True

    # Jump past the reset window.
    with patch(
        "app.circuit_breaker.time.monotonic", return_value=base + 11.0
    ):
        assert cb.is_open() is False, "past the reset window the probe goes through"
        cb.record_success()
        assert cb.is_open() is False
        assert cb.snapshot()["consecutive_failures"] == 0


def test_reset_window_re_opens_on_continued_failure():
    """Half-open probe failure → breaker re-opens for another window."""
    cb = CircuitBreaker(name="x", threshold=2, reset_after_seconds=10.0)
    base = 5000.0
    with patch("app.circuit_breaker.time.monotonic", return_value=base):
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open() is True

    with patch(
        "app.circuit_breaker.time.monotonic", return_value=base + 11.0
    ):
        assert cb.is_open() is False
        cb.record_failure()  # probe failed
        assert cb.is_open() is True
        # Counter preserved across re-opens — back-off is intentionally
        # longer when failures persist.
        assert cb.snapshot()["consecutive_failures"] == 3


def test_reset_force_closes():
    cb = CircuitBreaker(name="x", threshold=2)
    cb.record_failure()
    cb.record_failure()
    assert cb.is_open() is True
    cb.reset()
    assert cb.is_open() is False
    assert cb.snapshot()["consecutive_failures"] == 0


def test_snapshot_contains_diagnostic_fields():
    cb = CircuitBreaker(name="llm", threshold=4, reset_after_seconds=60.0)
    snap = cb.snapshot()
    assert snap["name"] == "llm"
    assert snap["threshold"] == 4
    assert snap["reset_after_seconds"] == 60.0
    assert snap["open"] is False
    assert snap["open_for_seconds_more"] is None

"""Unit tests for the retry/DLQ logic in scheduled_events repo.

The ``_compute_next_retry`` helper is the policy core. Tests
exercise both the happy path (each attempt produces a future
retry inside the schedule) and the exhaustion path (past the
last entry → None = DLQ).

The full ``mark_failed`` / ``fetch_due`` round-trip is integration-
tested against real Postgres in
tests/integration/test_scheduled_events_retry.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.db.repositories import scheduled_events as repo


def _fixed_now() -> datetime:
    return datetime(2026, 5, 8, 12, 0, 0, tzinfo=timezone.utc)


# ---- _compute_next_retry --------------------------------------------------


def test_first_failure_schedules_short_retry():
    """After the FIRST failure (attempt_count=1) the row should
    retry in ~1 minute. Catches transient blips quickly without
    hammering."""
    out = repo._compute_next_retry(attempt_count=1, now=_fixed_now())
    assert out is not None
    delta = out - _fixed_now()
    # First slot is 1 min ± 20% jitter.
    assert timedelta(seconds=48) <= delta <= timedelta(seconds=72)


def test_later_failures_extend_backoff():
    """The schedule grows: 1, 5, 15, 60, 240 min. Each successive
    failure pushes the next retry further out so a persistently-
    broken event doesn't busy-loop the dispatcher."""
    now = _fixed_now()
    second = repo._compute_next_retry(attempt_count=2, now=now)
    third = repo._compute_next_retry(attempt_count=3, now=now)
    assert second is not None and third is not None
    # Second retry should be ~5 min, third ~15 min — third > second.
    assert third > second
    # And both > 1 min after now.
    assert (second - now) > timedelta(minutes=2)
    assert (third - now) > timedelta(minutes=10)


def test_exhausted_attempts_returns_none():
    """Past the schedule end, ``_compute_next_retry`` returns
    None — that's the DLQ signal. Any caller using this to set
    ``next_retry_at`` will leave it NULL, which is what
    ``list_dlq`` filters for."""
    # Default schedule has 5 entries → max_attempts default = 6.
    # The 7th attempt is well past the budget.
    assert (
        repo._compute_next_retry(attempt_count=99, now=_fixed_now())
        is None
    )


def test_max_attempts_env_override(monkeypatch):
    """A deploy can tighten or loosen the retry budget via env
    without a code change. Setting max_attempts=2 means the
    first failure retries once; the second failure is DLQ."""
    monkeypatch.setenv("SCHEDULED_EVENT_MAX_ATTEMPTS", "2")
    # First failure (attempt_count=1) still retries.
    assert (
        repo._compute_next_retry(attempt_count=1, now=_fixed_now())
        is not None
    )
    # Second failure (attempt_count=2) → DLQ because 2 >= max.
    assert (
        repo._compute_next_retry(attempt_count=2, now=_fixed_now())
        is None
    )


def test_invalid_max_attempts_falls_back_to_default(monkeypatch):
    """Bogus env value (typo, accidental string) must not crash;
    fall back to the documented default."""
    monkeypatch.setenv("SCHEDULED_EVENT_MAX_ATTEMPTS", "not-a-number")
    # First failure should still retry under the default schedule.
    assert (
        repo._compute_next_retry(attempt_count=1, now=_fixed_now())
        is not None
    )


def test_attempt_count_zero_does_not_retry():
    """``attempt_count=0`` means we haven't even tried — should
    NOT compute a retry (only failure paths call this with >= 1)."""
    assert (
        repo._compute_next_retry(attempt_count=0, now=_fixed_now())
        is None
    )


def test_jitter_keeps_results_inside_band():
    """Run the helper many times and confirm every result falls
    in the documented ±20% jitter band. Without the band the
    dispatcher could see a thundering herd of retries at the
    exact same instant."""
    now = _fixed_now()
    deltas = [
        (repo._compute_next_retry(attempt_count=1, now=now) - now)
        for _ in range(50)
    ]
    # 1 min × 0.8 = 48s, × 1.2 = 72s.
    for d in deltas:
        assert timedelta(seconds=48) <= d <= timedelta(seconds=72)
    # And actual variance is non-zero — the helper isn't constant.
    assert len({d for d in deltas}) > 1

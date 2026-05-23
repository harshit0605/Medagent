"""Unit tests for the dose-reminder materializer (no DB, no HTTP)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from services.scheduler.dose_reminders import compute_dose_occurrences


def _utc(year, month, day, hour=0, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


def test_compute_occurrences_emits_each_time_in_window():
    """Two daily times across a 48h window → 4 occurrences."""
    schedule = {
        "type": "times_of_day",
        "times": ["08:00", "20:00"],
        "timezone": "UTC",
    }
    start = _utc(2026, 5, 3)
    end = _utc(2026, 5, 5)
    occurrences = compute_dose_occurrences(
        schedule, window_start=start, window_end=end
    )
    expected = [
        _utc(2026, 5, 3, 8, 0),
        _utc(2026, 5, 3, 20, 0),
        _utc(2026, 5, 4, 8, 0),
        _utc(2026, 5, 4, 20, 0),
    ]
    assert occurrences == expected


def test_compute_occurrences_handles_local_timezone_correctly():
    """A 09:00 IST dose on 2026-05-03 should land at 03:30 UTC the same day."""
    schedule = {
        "type": "times_of_day",
        "times": ["09:00"],
        "timezone": "Asia/Kolkata",
    }
    start = _utc(2026, 5, 3)
    end = _utc(2026, 5, 4)
    occurrences = compute_dose_occurrences(
        schedule, window_start=start, window_end=end
    )
    assert occurrences == [_utc(2026, 5, 3, 3, 30)]


def test_compute_occurrences_excludes_past_times_within_window_start():
    """If window_start is mid-day, the morning dose for that day is past — exclude."""
    schedule = {
        "type": "times_of_day",
        "times": ["08:00", "20:00"],
        "timezone": "UTC",
    }
    start = _utc(2026, 5, 3, 12, 0)  # noon — 08:00 has passed
    end = _utc(2026, 5, 4, 12, 0)
    occurrences = compute_dose_occurrences(
        schedule, window_start=start, window_end=end
    )
    assert occurrences == [
        _utc(2026, 5, 3, 20, 0),
        _utc(2026, 5, 4, 8, 0),
    ]


def test_compute_occurrences_skips_malformed_times():
    schedule = {
        "type": "times_of_day",
        "times": ["08:00", "not-a-time", "20:00"],
        "timezone": "UTC",
    }
    occurrences = compute_dose_occurrences(
        schedule,
        window_start=_utc(2026, 5, 3),
        window_end=_utc(2026, 5, 4),
    )
    assert occurrences == [
        _utc(2026, 5, 3, 8, 0),
        _utc(2026, 5, 3, 20, 0),
    ]


def test_compute_occurrences_returns_empty_for_unknown_schedule_type():
    assert (
        compute_dose_occurrences(
            {"type": "every_hours", "every_hours": 8},
            window_start=_utc(2026, 5, 3),
            window_end=_utc(2026, 5, 4),
        )
        == []
    )


def test_compute_occurrences_empty_when_no_times():
    schedule = {"type": "times_of_day", "times": [], "timezone": "UTC"}
    assert (
        compute_dose_occurrences(
            schedule,
            window_start=_utc(2026, 5, 3),
            window_end=_utc(2026, 5, 4),
        )
        == []
    )

"""Unit tests for the reminder-time-change parser (G1) — pure, no DB."""

from __future__ import annotations

from services.orchestrator.reminder_time_handler import (
    looks_like_time_change,
    parse_time_to_hhmm,
)


def test_parse_time_formats():
    assert parse_time_to_hhmm("change my reminder to 9am") == "09:00"
    assert parse_time_to_hhmm("remind me at 8:30 pm instead") == "20:30"
    assert parse_time_to_hhmm("set reminder to 21:00") == "21:00"
    assert parse_time_to_hhmm("move it to 12pm") == "12:00"  # noon
    assert parse_time_to_hhmm("make it 12am") == "00:00"  # midnight


def test_gate_true_for_retime_requests():
    assert looks_like_time_change("change my reminder to 9am") is True
    assert looks_like_time_change("remind me at 8pm instead") is True
    assert looks_like_time_change("move my dose time to 07:30") is True


def test_gate_false_for_non_retime():
    assert looks_like_time_change("used my reliever 3 times") is False
    assert looks_like_time_change("took my dose") is False
    assert looks_like_time_change("sugar 140") is False
    assert looks_like_time_change("") is False
    # Has a time but no retime context.
    assert looks_like_time_change("my appointment is at 9am") is False

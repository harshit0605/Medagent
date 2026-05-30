"""Unit tests for the pregnancy NL parsers (E5/E6) — pure, no DB."""

from __future__ import annotations

from datetime import date

from services.orchestrator.pregnancy_nl_handler import (
    looks_like_pregnancy_intake,
    looks_like_pregnancy_nl,
    looks_like_pregnancy_status_query,
    parse_lmp_date,
)

_ON = date(2026, 5, 27)


def test_parse_lmp_various_formats():
    assert parse_lmp_date("pregnant, LMP 15 Jan", on=_ON) == date(2026, 1, 15)
    assert parse_lmp_date("last period 15/01/2026", on=_ON) == date(2026, 1, 15)
    assert parse_lmp_date("lmp 2026-03-01", on=_ON) == date(2026, 3, 1)
    assert parse_lmp_date("last period was January 15", on=_ON) == date(2026, 1, 15)
    assert parse_lmp_date("period started 2 March", on=_ON) == date(2026, 3, 2)


def test_parse_lmp_rejects_implausible():
    # No date at all.
    assert parse_lmp_date("I think I'm pregnant", on=_ON) is None
    # Way too old (> 44 weeks) — not a current-pregnancy LMP.
    assert parse_lmp_date("lmp 2024-01-01", on=_ON) is None


def test_intake_gate_requires_pregnant_plus_lmp_plus_date():
    assert looks_like_pregnancy_intake("pregnant, LMP 15 Jan") is True
    # "pregnant" but no LMP anchor → not an intake (could be a question).
    assert looks_like_pregnancy_intake("am I still pregnant?") is False
    # LMP context but no parseable date.
    assert looks_like_pregnancy_intake("pregnant, last period a while ago") is False


def test_status_query_gate():
    assert looks_like_pregnancy_status_query("how many weeks am I?") is True
    assert looks_like_pregnancy_status_query("what's next in my pregnancy") is True
    assert looks_like_pregnancy_status_query("pregnancy checklist") is True
    assert looks_like_pregnancy_status_query("when is my next scan") is True
    assert looks_like_pregnancy_status_query("sugar 140") is False
    assert looks_like_pregnancy_status_query("") is False


def test_combined_gate():
    assert looks_like_pregnancy_nl("pregnant, LMP 15 Jan") is True
    assert looks_like_pregnancy_nl("how far along am I?") is True
    assert looks_like_pregnancy_nl("just saying hi") is False

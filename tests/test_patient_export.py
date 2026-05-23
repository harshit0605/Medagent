"""Unit tests for patient_export helpers (no DB).

Covers the small pure-function helpers that don't need a real
database — ``_iso`` for datetime/date normalisation and
``_enum_value`` for enum unwrapping. The big ``build_patient_export``
function is integration-tested against a real Postgres in
tests/integration/test_patient_export.py.
"""

from __future__ import annotations

import enum
from datetime import date, datetime, timezone

from services.orchestrator import patient_export as pe


# ---- _iso ---------------------------------------------------------------


def test_iso_renders_naive_datetime_as_utc():
    """Postgres rows occasionally return naive datetimes; the
    export helper must normalise to UTC rather than emitting an
    ambiguous string. Without this guard, a regulator reading the
    export couldn't tell what timezone the timestamps are in."""
    naive = datetime(2026, 5, 7, 12, 0, 0)
    out = pe._iso(naive)
    assert out is not None
    assert "+00:00" in out


def test_iso_preserves_tz_aware_datetime():
    aware = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)
    out = pe._iso(aware)
    assert out == "2026-05-07T12:00:00+00:00"


def test_iso_handles_date_only():
    """Schedule + supply tracking columns are plain dates (no time
    component). The export shouldn't synthesise a 00:00 time —
    just emit YYYY-MM-DD."""
    assert pe._iso(date(2026, 5, 7)) == "2026-05-07"


def test_iso_returns_none_for_none():
    assert pe._iso(None) is None


# ---- _enum_value --------------------------------------------------------


class _ToyEnum(enum.Enum):
    foo = "foo_string"
    bar = "bar_string"


def test_enum_value_unwraps_enum():
    assert pe._enum_value(_ToyEnum.foo) == "foo_string"


def test_enum_value_passes_through_strings_and_none():
    assert pe._enum_value("plain") == "plain"
    assert pe._enum_value(None) is None
    assert pe._enum_value(42) == 42

"""Unit tests for the pregnancy timeline math + milestone schedule (no DB).

These are the deterministic core: gestational-age math, EDD derivation,
trimester boundaries, and the milestone/weekly schedule. Fully unit-testable
without a clock or database.
"""

from __future__ import annotations

from datetime import date

from services.orchestrator import pregnancy as preg


# ---- date math -------------------------------------------------------------


def test_edd_lmp_roundtrip():
    lmp = date(2026, 1, 1)
    edd = preg.edd_from_lmp(lmp)
    assert edd == date(2026, 10, 8)  # +280 days
    assert preg.lmp_from_edd(edd) == lmp


def test_resolve_lmp_edd_fills_missing():
    lmp = date(2026, 1, 1)
    # lmp only → edd derived
    assert preg.resolve_lmp_edd(lmp, None) == (lmp, date(2026, 10, 8))
    # edd only → lmp derived
    assert preg.resolve_lmp_edd(None, date(2026, 10, 8)) == (lmp, date(2026, 10, 8))
    # both → returned as-is (explicit values win)
    custom_edd = date(2026, 10, 15)
    assert preg.resolve_lmp_edd(lmp, custom_edd) == (lmp, custom_edd)


def test_resolve_lmp_edd_requires_one():
    import pytest

    with pytest.raises(ValueError):
        preg.resolve_lmp_edd(None, None)


def test_gestational_age():
    lmp = date(2026, 1, 1)
    # 90 days later = 12 weeks 6 days
    assert preg.gestational_age(lmp, date(2026, 4, 1)) == (12, 6)
    # exactly 14 weeks
    assert preg.gestational_age(lmp, date(2026, 4, 9)) == (14, 0)
    # same day = 0,0
    assert preg.gestational_age(lmp, lmp) == (0, 0)
    # date before LMP clamps to 0,0 (defensive)
    assert preg.gestational_age(lmp, date(2025, 12, 1)) == (0, 0)


def test_trimester_boundaries():
    assert preg.trimester(0) == 1
    assert preg.trimester(13) == 1
    assert preg.trimester(14) == 2
    assert preg.trimester(27) == 2
    assert preg.trimester(28) == 3
    assert preg.trimester(41) == 3


# ---- milestone schedule ----------------------------------------------------


def test_milestone_dates_sorted_and_anchored():
    lmp = date(2026, 1, 1)
    pairs = preg.milestone_dates(lmp)
    assert len(pairs) == len(preg.MILESTONES)
    # sorted ascending by date
    dates = [d for _m, d in pairs]
    assert dates == sorted(dates)
    # anchored: a week-20 milestone lands 140 days after LMP
    anomaly = next(m for m, _d in pairs if m.key == "scan_anomaly")
    target = next(d for m, d in pairs if m.key == "scan_anomaly")
    assert anomaly.week == 20
    assert target == date(2026, 5, 21)  # 140 days


def test_upcoming_and_next_milestone_filtering():
    lmp = date(2026, 1, 1)
    on = date(2026, 4, 1)  # ~week 12-13; week 8 + 12 milestones are past
    upcoming = preg.upcoming_milestones(lmp, on=on)
    keys = {m.key for m, _d in upcoming}
    assert "lab_booking" not in keys  # week 8 — past
    assert "scan_anomaly" in keys  # week 20 — future
    # all upcoming are strictly after `on`
    assert all(d > on for _m, d in upcoming)
    nm = preg.next_milestone(lmp, on=on)
    assert nm is not None
    # soonest upcoming is the earliest-dated future milestone
    assert nm[1] == min(d for _m, d in upcoming)


def test_next_milestone_none_past_term():
    lmp = date(2026, 1, 1)
    # well past the last milestone (week 40 ~ 2026-10-08)
    assert preg.next_milestone(lmp, on=date(2027, 1, 1)) is None


def test_next_weekly_checkins():
    lmp = date(2026, 1, 1)
    on = date(2026, 4, 1)  # completed week 12
    checkins = preg.next_weekly_checkins(lmp, on=on, count=2)
    assert [w for w, _d in checkins] == [13, 14]
    # all strictly after `on`
    assert all(d > on for _w, d in checkins)
    # capped at full term — none beyond week 40
    late = preg.next_weekly_checkins(lmp, on=date(2026, 10, 1), count=5)
    assert all(w <= preg.FULL_TERM_WEEKS for w, _d in late)


def test_weekly_focus_non_empty():
    for week in (4, 8, 12, 16, 20, 28, 36, 40):
        assert preg.weekly_focus(week).strip()

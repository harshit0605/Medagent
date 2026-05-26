"""Pure-math unit tests for the postpartum timeline module.

No DB / no clock — every function in :mod:`services.orchestrator.postpartum`
is a pure function of input dates.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from services.orchestrator import postpartum as pp


# ---- date math --------------------------------------------------------------


def test_postpartum_days_zero_on_delivery_day():
    delivery = date(2026, 1, 10)
    assert pp.postpartum_days(delivery, delivery) == 0


def test_postpartum_days_advances():
    delivery = date(2026, 1, 10)
    assert pp.postpartum_days(delivery, date(2026, 1, 17)) == 7
    assert pp.postpartum_days(delivery, date(2026, 2, 21)) == 42


def test_postpartum_days_clamped_at_zero_for_pre_delivery():
    delivery = date(2026, 1, 10)
    assert pp.postpartum_days(delivery, date(2026, 1, 5)) == 0


def test_postpartum_week_division():
    delivery = date(2026, 1, 10)
    assert pp.postpartum_week(delivery, date(2026, 1, 10)) == 0
    assert pp.postpartum_week(delivery, date(2026, 1, 16)) == 0
    assert pp.postpartum_week(delivery, date(2026, 1, 17)) == 1
    assert pp.postpartum_week(delivery, date(2026, 2, 21)) == 6


def test_in_postpartum_window():
    delivery = date(2026, 1, 10)
    assert pp.in_postpartum_window(delivery, delivery) is True
    # Last day inside the 12-week window (day 83 < 84).
    assert pp.in_postpartum_window(
        delivery, delivery + timedelta(days=83)
    ) is True
    # First day outside.
    assert pp.in_postpartum_window(
        delivery, delivery + timedelta(days=84)
    ) is False


# ---- milestone schedule -----------------------------------------------------


def test_milestone_dates_sorted_chronologically():
    delivery = date(2026, 1, 10)
    pairs = pp.milestone_dates(delivery)
    dates = [d for (_m, d) in pairs]
    assert dates == sorted(dates), "milestones must be in chronological order"


def test_milestone_dates_have_all_template_entries():
    delivery = date(2026, 1, 10)
    pairs = pp.milestone_dates(delivery)
    keys = {m.key for (m, _d) in pairs}
    # The schedule covers early visit, day-6-8 visit, two EPDS screens,
    # 6-week visit + contraception, baby vaccines, final close.
    assert {
        "pp_visit_early",
        "pp_visit_week1",
        "pp_epds_d14",
        "pp_visit_6w",
        "pp_epds_6w",
        "pp_contraception",
        "pp_baby_vax_8w",
        "pp_close",
    } == keys


def test_upcoming_milestones_filters_past():
    delivery = date(2026, 1, 10)
    # On day 30: the 2-day, 7-day, and 14-day items are past; rest are upcoming.
    on = delivery + timedelta(days=30)
    upcoming = pp.upcoming_milestones(delivery, on=on)
    keys = [m.key for (m, _d) in upcoming]
    assert "pp_visit_early" not in keys
    assert "pp_visit_week1" not in keys
    assert "pp_epds_d14" not in keys
    assert "pp_visit_6w" in keys
    assert "pp_baby_vax_8w" in keys


def test_next_milestone_returns_the_soonest():
    delivery = date(2026, 1, 10)
    nm = pp.next_milestone(delivery, on=delivery)
    assert nm is not None
    milestone, target = nm
    # Earliest milestone is day 2 (early PP check).
    assert milestone.key == "pp_visit_early"
    assert target == delivery + timedelta(days=2)


def test_next_milestone_none_past_window():
    delivery = date(2026, 1, 10)
    nm = pp.next_milestone(
        delivery, on=delivery + timedelta(days=200)
    )
    assert nm is None


# ---- weekly check-ins -------------------------------------------------------


def test_next_weekly_checkins_starts_at_current_plus_one():
    delivery = date(2026, 1, 10)
    # On day 0 we're in week 0; next check-in is week 1 (day 7).
    out = pp.next_weekly_checkins(delivery, on=delivery, count=2)
    weeks = [w for (w, _d) in out]
    assert weeks == [1, 2]
    # Targets are anchored on delivery + N*7.
    assert out[0][1] == delivery + timedelta(days=7)
    assert out[1][1] == delivery + timedelta(days=14)


def test_next_weekly_checkins_caps_at_phase_boundary():
    delivery = date(2026, 1, 10)
    # On week 11 we should only get week 12 (the boundary), then stop.
    on = delivery + timedelta(days=11 * 7)
    out = pp.next_weekly_checkins(delivery, on=on, count=4)
    weeks = [w for (w, _d) in out]
    assert weeks == [12]


def test_next_weekly_checkins_empty_past_window():
    delivery = date(2026, 1, 10)
    out = pp.next_weekly_checkins(
        delivery, on=delivery + timedelta(days=200), count=2
    )
    assert out == []


# ---- weekly focus copy ------------------------------------------------------


def test_weekly_focus_has_a_line_for_every_in_window_week():
    # Sanity: no week returns empty, and the 6-week line is special.
    for week in range(0, pp.POSTPARTUM_PHASE_WEEKS):
        line = pp.weekly_focus(week)
        assert isinstance(line, str) and len(line) > 0


def test_weekly_focus_six_week_callout():
    line = pp.weekly_focus(6)
    # 6-week message references the visit + contraception conversation.
    assert "contraception" in line.lower() or "6 weeks" in line.lower()


@pytest.mark.parametrize(
    "kind",
    sorted({m.kind for m in pp.MILESTONES}),
)
def test_milestone_kinds_are_one_of_known_set(kind):
    assert kind in {"visit", "screen", "vaccine", "counsel"}

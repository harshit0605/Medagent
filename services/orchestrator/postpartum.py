"""Postpartum timeline math + milestone schedule (pure, no I/O).

Mirrors :mod:`services.orchestrator.pregnancy` for the post-delivery phase:
given a delivery date it computes postpartum age (days / weeks) and the
postpartum-care milestone schedule (early PP check, mental-health screen,
6-week visit + contraception, pediatric vaccine nudges, final close).

Everything here is a pure function so it's fully unit-testable without a DB
or clock. The materializer
(:mod:`services.scheduler.postpartum_milestones`) turns these into scheduled
reminders; the dispatcher renders them into sends.

Clinical scope: logistics + screening reminders only — the same
non-diagnostic stance as the pregnancy module. A clinician's individualized
plan always overrides this default cadence.

Phase boundary: we materialize for the first ``POSTPARTUM_PHASE_WEEKS`` weeks
(12 weeks). Beyond that, the postpartum-active row should be closed via
``end_postpartum`` — long-term care continues through normal regimens / care
plans, not the PP-specific cadence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

# Standard postpartum care window. The 12-week boundary aligns with most
# obstetric guidelines (the "fourth trimester"). Past this point we stop
# materializing PP reminders.
POSTPARTUM_PHASE_WEEKS = 12
POSTPARTUM_PHASE_DAYS = POSTPARTUM_PHASE_WEEKS * 7


# ---- core date math --------------------------------------------------------


def postpartum_days(delivery_date: date, on: date) -> int:
    """Days since delivery (clamped at zero — pre-delivery dates yield 0)."""
    return max(0, (on - delivery_date).days)


def postpartum_week(delivery_date: date, on: date) -> int:
    """Completed postpartum weeks on a given date."""
    return postpartum_days(delivery_date, on) // 7


def in_postpartum_window(delivery_date: date, on: date) -> bool:
    """True iff ``on`` falls inside the 12-week postpartum window."""
    days = postpartum_days(delivery_date, on)
    return 0 <= days < POSTPARTUM_PHASE_DAYS


# ---- milestone schedule ----------------------------------------------------


@dataclass(frozen=True)
class PostpartumMilestone:
    """One postpartum-care milestone, anchored on day-since-delivery.

    ``key`` is a stable identifier used for idempotent scheduling (the sweep
    dedupes on it). ``kind`` is one of visit / screen / vaccine / counsel.
    """

    day: int
    kind: str
    key: str
    title: str
    detail: str


# Postpartum care template — conservative, widely-applicable. A clinician's
# individualized plan overrides. Mental-health screen at day 14 and again
# at 6 weeks is the EPDS standard (Edinburgh Postnatal Depression Scale).
MILESTONES: tuple[PostpartumMilestone, ...] = (
    PostpartumMilestone(
        2, "visit", "pp_visit_early",
        "Early postpartum check",
        "an early check-in (days 1-3) — sleep, feeding, bleeding, pain.",
    ),
    PostpartumMilestone(
        7, "visit", "pp_visit_week1",
        "Day 6-8 postnatal visit",
        "your first scheduled postnatal visit — wound check (if applicable), "
        "lactation, and baby weight.",
    ),
    PostpartumMilestone(
        14, "screen", "pp_epds_d14",
        "Mental-health check (EPDS)",
        "a short mood + wellbeing check-in (this is routine — there are no "
        "wrong answers).",
    ),
    PostpartumMilestone(
        42, "visit", "pp_visit_6w",
        "6-week postnatal visit",
        "your 6-week postnatal visit + contraception conversation.",
    ),
    PostpartumMilestone(
        42, "screen", "pp_epds_6w",
        "Mental-health check (EPDS, 6-week)",
        "a follow-up mood check-in at 6 weeks — your care team uses this "
        "to plan ongoing support.",
    ),
    PostpartumMilestone(
        42, "counsel", "pp_contraception",
        "Contraception conversation",
        "a brief check-in about contraception options now that you're 6 "
        "weeks postpartum.",
    ),
    PostpartumMilestone(
        56, "vaccine", "pp_baby_vax_8w",
        "Baby's 8-week vaccines",
        "a reminder about your baby's 8-week vaccinations (DTP / HepB / "
        "polio / Hib / pneumococcal — check with your paediatrician).",
    ),
    PostpartumMilestone(
        84, "visit", "pp_close",
        "Final postnatal review",
        "the closing 12-week postpartum review — we'll check in then close "
        "out the postnatal cadence.",
    ),
)


def milestone_dates(
    delivery_date: date,
) -> list[tuple[PostpartumMilestone, date]]:
    """All PP milestones paired with their target calendar date.

    Target date = delivery_date + day-offset. Sorted by date (then key for
    stable ordering when multiple share a day, e.g. the three 6-week items).
    """
    pairs = [
        (m, delivery_date + timedelta(days=m.day)) for m in MILESTONES
    ]
    pairs.sort(key=lambda p: (p[1], p[0].key))
    return pairs


def upcoming_milestones(
    delivery_date: date, *, on: date
) -> list[tuple[PostpartumMilestone, date]]:
    """Milestones whose target date is strictly after ``on``."""
    return [(m, d) for (m, d) in milestone_dates(delivery_date) if d > on]


def next_milestone(
    delivery_date: date, *, on: date
) -> tuple[PostpartumMilestone, date] | None:
    """The single soonest still-upcoming PP milestone, or ``None``."""
    upcoming = upcoming_milestones(delivery_date, on=on)
    return upcoming[0] if upcoming else None


def next_weekly_checkins(
    delivery_date: date, *, on: date, count: int = 2
) -> list[tuple[int, date]]:
    """The next ``count`` weekly PP check-in boundaries as ``(week, date)``.

    Capped at ``POSTPARTUM_PHASE_WEEKS`` — beyond that the PP cadence ends
    and we stop nudging. Boundaries align on delivery_date + N×7 days.
    """
    current = postpartum_week(delivery_date, on)
    out: list[tuple[int, date]] = []
    week = current + 1
    while len(out) < count and week <= POSTPARTUM_PHASE_WEEKS:
        target = delivery_date + timedelta(days=week * 7)
        if target > on:
            out.append((week, target))
        week += 1
    return out


def weekly_focus(week: int) -> str:
    """A short non-diagnostic 'what to expect this week' line for the
    weekly postpartum check-in."""
    if week <= 1:
        return (
            "Early days — rest when you can, drink fluids, and reach out if "
            "anything feels off."
        )
    if week <= 3:
        return (
            "First few weeks — feeding rhythm, bleeding tapering, and watch "
            "for signs of infection. Reply HELP if anything worries you."
        )
    if week == 6:
        return (
            "6 weeks in — your postnatal visit + contraception chat are "
            "coming up. Mental-health check too: it's routine."
        )
    if week < 6:
        return (
            "Settling in — feeding, sleep snippets, mood ups and downs are "
            "all normal. Reach out if anything stays heavy."
        )
    if week < POSTPARTUM_PHASE_WEEKS:
        return (
            "You're in the back half of your postpartum window — keep up "
            "your visits, and let us know how you're doing."
        )
    return (
        "You're at the 12-week mark — we'll wrap up the postnatal cadence. "
        "Care continues through your regular regimens."
    )

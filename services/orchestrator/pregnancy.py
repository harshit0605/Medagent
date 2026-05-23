"""Pregnancy timeline math + milestone schedule (pure, no I/O).

This is the deterministic core of the pregnancy timeline engine: given a
last-menstrual-period (LMP) date it computes gestational age, the estimated
due date (EDD), the trimester, and the antenatal-care milestone schedule
(visits / labs / scans / supplements) anchored on gestational week.

Everything here is a pure function so it's fully unit-testable without a DB
or clock. The milestone materializer (:mod:`services.scheduler.pregnancy_milestones`)
turns these into scheduled reminders; the dispatcher renders them into sends.

Clinical scope: this drives *logistics* reminders (when to book a scan, take
a supplement, attend a visit) — NOT diagnosis. The schedule is a simplified,
widely-applicable ANC template; a clinician's plan always overrides.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

# Naegele's rule: EDD = LMP + 280 days (40 weeks).
GESTATION_DAYS = 280
FULL_TERM_WEEKS = 40


# ---- core date math --------------------------------------------------------


def edd_from_lmp(lmp: date) -> date:
    """Estimated due date from last menstrual period (Naegele's rule)."""
    return lmp + timedelta(days=GESTATION_DAYS)


def lmp_from_edd(edd: date) -> date:
    """Back out the LMP from a known due date."""
    return edd - timedelta(days=GESTATION_DAYS)


def resolve_lmp_edd(
    lmp: date | None, edd: date | None
) -> tuple[date, date]:
    """Return a complete ``(lmp, edd)`` pair, deriving whichever is missing.

    At least one must be provided; raises ``ValueError`` otherwise. When both
    are given they're returned as-is (the caller's explicit values win — a
    clinician may set an EDD adjusted by a dating scan that differs from the
    LMP-derived one).
    """
    if lmp is not None and edd is not None:
        return lmp, edd
    if lmp is not None:
        return lmp, edd_from_lmp(lmp)
    if edd is not None:
        return lmp_from_edd(edd), edd
    raise ValueError("at least one of lmp or edd is required")


def gestational_age(lmp: date, on: date) -> tuple[int, int]:
    """Gestational age as ``(weeks, days)`` on a given date.

    Clamped at zero — a date before the LMP yields ``(0, 0)`` rather than a
    negative age (defensive; the engine only runs on plausible records).
    """
    total_days = max(0, (on - lmp).days)
    return total_days // 7, total_days % 7


def gestational_weeks(lmp: date, on: date) -> int:
    """Completed gestational weeks on a given date."""
    return gestational_age(lmp, on)[0]


def trimester(weeks: int) -> int:
    """Trimester for a completed-week count.

    T1: weeks 0–13, T2: weeks 14–27, T3: weeks 28+. Past 42 weeks we still
    report T3 (post-term is a clinical matter, not a fourth trimester here).
    """
    if weeks < 14:
        return 1
    if weeks < 28:
        return 2
    return 3


# ---- milestone schedule ----------------------------------------------------


@dataclass(frozen=True)
class Milestone:
    """One scheduled antenatal-care milestone, anchored on gestational week.

    ``key`` is a stable identifier used for idempotent scheduling (the sweep
    dedupes on it). ``kind`` is one of visit / lab / scan / supplement.
    """

    week: int
    kind: str
    key: str
    title: str
    detail: str


# Simplified, widely-applicable ANC milestone template. Multiple milestones
# can share a gestational week (e.g. week 20 = anomaly scan + a visit +
# starting calcium) — each carries a distinct ``key`` so they schedule and
# dedupe independently. A clinician's individualized plan overrides this.
MILESTONES: tuple[Milestone, ...] = (
    # First trimester
    Milestone(8, "scan", "scan_dating", "Dating scan",
              "an early ultrasound to confirm your due date"),
    Milestone(8, "lab", "lab_booking", "Booking blood tests",
              "your first set of pregnancy blood tests (blood group, "
              "haemoglobin, blood sugar, and infection screen)"),
    Milestone(12, "scan", "scan_nt", "NT scan",
              "the 11–13 week nuchal-translucency ultrasound"),
    Milestone(12, "visit", "visit_12", "First antenatal check-up",
              "your first full antenatal visit with the clinic"),
    Milestone(12, "supplement", "supp_ifa", "Iron & folic acid",
              "starting your daily iron + folic acid supplement"),
    # Second trimester
    Milestone(20, "scan", "scan_anomaly", "Anomaly scan (TIFFA)",
              "the detailed 18–22 week anatomy ultrasound"),
    Milestone(20, "visit", "visit_20", "Antenatal visit",
              "your 20-week antenatal check-up"),
    Milestone(20, "supplement", "supp_calcium", "Calcium supplement",
              "starting your daily calcium supplement"),
    Milestone(26, "lab", "lab_gtt", "Glucose tolerance test",
              "the gestational-diabetes screening blood test"),
    # Third trimester
    Milestone(28, "lab", "lab_hb28", "Repeat blood tests",
              "a repeat haemoglobin and antibody check"),
    Milestone(28, "visit", "visit_28", "Antenatal visit",
              "your 28-week antenatal check-up"),
    Milestone(32, "scan", "scan_growth", "Growth scan",
              "a third-trimester ultrasound to check baby's growth"),
    Milestone(32, "visit", "visit_32", "Antenatal visit",
              "your 32-week antenatal check-up"),
    Milestone(36, "lab", "lab_gbs", "Group B Strep swab",
              "the 36-week Group B Streptococcus screening swab"),
    Milestone(36, "visit", "visit_36", "Antenatal visit",
              "your 36-week antenatal check-up"),
    Milestone(38, "visit", "visit_38", "Antenatal visit",
              "your 38-week antenatal check-up"),
    Milestone(40, "visit", "visit_40", "Term check-up",
              "your due-date check-up"),
)


def milestone_dates(lmp: date) -> list[tuple[Milestone, date]]:
    """All milestones paired with their target calendar date for this LMP.

    Target date = LMP + (week × 7 days). Sorted by date (then key for a
    stable order when several fall on the same day).
    """
    pairs = [
        (m, lmp + timedelta(days=m.week * 7))
        for m in MILESTONES
    ]
    pairs.sort(key=lambda p: (p[1], p[0].key))
    return pairs


def upcoming_milestones(
    lmp: date, *, on: date
) -> list[tuple[Milestone, date]]:
    """Milestones whose target date is strictly after ``on`` (still schedulable)."""
    return [(m, d) for (m, d) in milestone_dates(lmp) if d > on]


def next_milestone(lmp: date, *, on: date) -> tuple[Milestone, date] | None:
    """The single soonest still-upcoming milestone, or ``None`` past term."""
    upcoming = upcoming_milestones(lmp, on=on)
    return upcoming[0] if upcoming else None


def next_weekly_checkins(
    lmp: date, *, on: date, count: int = 2
) -> list[tuple[int, date]]:
    """The next ``count`` weekly check-in boundaries as ``(week, date)``.

    A check-in for completed week N lands on LMP + N×7 days. We return the
    upcoming boundaries strictly after ``on``, capped at full term (we don't
    nudge a weekly check-in past 40 weeks — by then it's about delivery, not
    weekly milestones).
    """
    current = gestational_weeks(lmp, on)
    out: list[tuple[int, date]] = []
    week = current + 1
    while len(out) < count and week <= FULL_TERM_WEEKS:
        target = lmp + timedelta(days=week * 7)
        if target > on:
            out.append((week, target))
        week += 1
    return out


def weekly_focus(week: int) -> str:
    """A short, non-diagnostic 'what's happening this week' line for the
    weekly check-in. Trimester-based with a few week-specific highlights."""
    highlights = {
        8: "Your dating scan and first blood tests are coming up soon.",
        12: "Time for your NT scan and first full antenatal visit — and to "
            "start iron & folic acid if you haven't.",
        16: "Many people start to feel more energetic this trimester.",
        20: "Your detailed anomaly scan (TIFFA) is due around now.",
        24: "Your glucose tolerance test is coming up in the next couple of weeks.",
        28: "Third trimester — repeat blood tests and more frequent visits ahead.",
        32: "A growth scan is due around now.",
        36: "Group B Strep swab and weekly visits begin around now.",
        40: "You're at term — keep an eye out for signs of labour.",
    }
    if week in highlights:
        return highlights[week]
    tri = trimester(week)
    if tri == 1:
        return ("Keep taking your supplements and stay hydrated. Reply with "
                "any questions for your care team.")
    if tri == 2:
        return ("Keep up your antenatal visits and supplements. Reply with "
                "any questions for your care team.")
    return ("You're in the home stretch — keep up your visits and rest well. "
            "Reply with any questions for your care team.")

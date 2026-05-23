"""Post-op recovery checklist schedule + day math (pure, no I/O).

Anchored on the surgery date: computes the post-op day and the day-N checklist
(wound check, suture removal, follow-up, recovery review) plus a wound-photo
review prompt. Drives logistics reminders only — not clinical assessment.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


@dataclass(frozen=True)
class PostOpCheck:
    day: int
    key: str
    title: str
    detail: str


# Simplified, widely-applicable post-op checklist (day = days after surgery).
CHECKLIST: tuple[PostOpCheck, ...] = (
    PostOpCheck(1, "day1_check", "Day-1 check-in",
                "how you're feeling and your pain level"),
    PostOpCheck(2, "wound_photo", "Wound photo",
                "a photo of your wound so the team can check healing"),
    PostOpCheck(3, "day3_wound", "Wound check",
                "signs of infection — redness, swelling, discharge, or fever"),
    PostOpCheck(7, "suture", "Suture / staple removal",
                "your suture or staple removal, if your surgeon advised it"),
    PostOpCheck(7, "followup", "Follow-up visit",
                "your post-op follow-up appointment"),
    PostOpCheck(14, "review", "Recovery review",
                "a final recovery review with your clinician"),
)


def post_op_day(surgery_date: date, on: date) -> int:
    """Days since surgery (clamped at 0)."""
    return max(0, (on - surgery_date).days)


def check_dates(surgery_date: date) -> list[tuple[PostOpCheck, date]]:
    """All checklist items paired with their target date, sorted by date."""
    pairs = [
        (c, surgery_date + timedelta(days=c.day)) for c in CHECKLIST
    ]
    pairs.sort(key=lambda p: (p[1], p[0].key))
    return pairs


def upcoming_checks(
    surgery_date: date, *, on: date
) -> list[tuple[PostOpCheck, date]]:
    """Checklist items whose target date is strictly after ``on``."""
    return [(c, d) for (c, d) in check_dates(surgery_date) if d > on]


def next_check(
    surgery_date: date, *, on: date
) -> tuple[PostOpCheck, date] | None:
    upcoming = upcoming_checks(surgery_date, on=on)
    return upcoming[0] if upcoming else None

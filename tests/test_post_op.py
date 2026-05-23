"""Unit tests for the post-op schedule math + wound-photo marker (no DB)."""

from __future__ import annotations

from datetime import date

from services.orchestrator import post_op as p
from services.orchestrator.wound_photo_handler import looks_like_wound_photo


def test_post_op_day_clamped():
    s = date(2026, 1, 1)
    assert p.post_op_day(s, date(2026, 1, 8)) == 7
    assert p.post_op_day(s, s) == 0
    assert p.post_op_day(s, date(2025, 12, 25)) == 0


def test_check_dates_sorted_and_complete():
    pairs = p.check_dates(date(2026, 1, 1))
    assert len(pairs) == len(p.CHECKLIST)
    dates = [d for _c, d in pairs]
    assert dates == sorted(dates)
    # day-2 wound-photo lands 2 days post-op
    wound = next(c for c, _d in pairs if c.key == "wound_photo")
    target = next(d for c, d in pairs if c.key == "wound_photo")
    assert wound.day == 2
    assert target == date(2026, 1, 3)


def test_next_check():
    s = date(2026, 1, 1)
    nc = p.next_check(s, on=date(2026, 1, 4))  # day 3 → next is day 7
    assert nc is not None and nc[0].day == 7
    assert p.next_check(s, on=date(2026, 3, 1)) is None  # past all checks


def test_wound_photo_marker():
    assert looks_like_wound_photo(
        "[wound-photo] public_path=/u/x.jpg mime=image/jpeg"
    )
    assert looks_like_wound_photo("[wound-photo] public_path=/u/x.jpg")
    assert not looks_like_wound_photo("[prescription-upload] public_path=/u/x.jpg")
    assert not looks_like_wound_photo("just text")
    assert not looks_like_wound_photo(None)

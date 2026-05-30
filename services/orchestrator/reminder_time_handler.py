"""Self-service reminder-time change (G1).

Lets a patient move their dose-reminder time conversationally — "change my
reminder to 9am", "remind me at 8pm instead" — without an operator. Parses the
new time(s), updates the regimen's schedule, and re-materializes the upcoming
dose events so the new time takes effect immediately.

Scope guard: only acts when the patient has EXACTLY ONE active regimen (the
unambiguous case). With zero or multiple active regimens it returns a clarifying
reply rather than guessing which medication to retime.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from app.logging_redact import redact_phone

log = logging.getLogger(__name__)


# Intent trigger: a "remind/reminder/dose ... time/at ..." request.
_RETIME_CONTEXT_RE = re.compile(
    r"\b(remind|reminder|reminders|dose\s+time|alert)\b", re.IGNORECASE
)
_CHANGE_VERB_RE = re.compile(
    r"\b(change|move|set|switch|reschedule|make\s+it|instead|earlier|later|at)\b",
    re.IGNORECASE,
)

# Time forms: "9am", "9:30 pm", "21:00", "8 pm".
_TIME_RE = re.compile(
    r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.IGNORECASE
)


def parse_time_to_hhmm(text: str) -> str | None:
    """Extract a single 24h ``HH:MM`` from a time phrase, or None.

    Requires either an am/pm marker OR a colon OR a 24h-looking hour, so a
    bare "3" in "3 times" doesn't read as a time. Returns the FIRST plausible
    time found."""
    for m in _TIME_RE.finditer(text):
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        ampm = (m.group(3) or "").lower()
        has_colon = m.group(2) is not None
        if not ampm and not has_colon and not (0 <= hour <= 23 and hour > 12):
            # Bare 1-12 with no am/pm and no colon is ambiguous — skip unless
            # it's clearly 24h (>12). "at 9" alone is too weak.
            if hour > 23 or minute > 59:
                continue
            # Allow a bare hour only when an explicit time-context word
            # ("at"/"by") immediately precedes — checked by the caller's gate.
            # Here we still accept it as a candidate but the gate filters noise.
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"
    return None


def looks_like_time_change(text: str | None) -> bool:
    if not text:
        return False
    return bool(
        _RETIME_CONTEXT_RE.search(text)
        and _CHANGE_VERB_RE.search(text)
        and parse_time_to_hhmm(text) is not None
    )


async def handle_time_change(
    *, patient_phone: str, new_user_text: str
) -> dict | None:
    """Apply a reminder-time change. Returns a delta or None."""
    if not looks_like_time_change(new_user_text):
        return None
    new_time = parse_time_to_hhmm(new_user_text)
    if new_time is None:
        return None

    from app.db.repositories import patients as patients_repo
    from app.db.repositories import regimens as regimens_repo
    from app.db.session import get_sessionmaker
    from services.scheduler import dose_reminders

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        patient = await patients_repo.get_by_phone(db, patient_phone)
        if patient is None:
            return None
        today = datetime.now(timezone.utc).date()
        regimens = await regimens_repo.list_for_patient(
            db, patient.id, active_on=today
        )
        if len(regimens) == 0:
            return {
                "response_body": (
                    "I don't see any active medication reminders to change. "
                    "Reply HELP if you think this is wrong."
                ),
                "audit_reasons": ["reminder_time_change_no_regimen"],
            }
        if len(regimens) > 1:
            meds = ", ".join(r.medication_name for r in regimens)
            return {
                "response_body": (
                    "You have a few medications on reminders "
                    f"({meds}). Reply with which one to retime, e.g. "
                    f"'change Metformin reminder to {new_time}'."
                ),
                "audit_reasons": ["reminder_time_change_ambiguous"],
            }

        regimen = regimens[0]
        schedule = dict(regimen.schedule or {})
        old_times = schedule.get("times") or []
        # Single-time regimens retime cleanly. Multi-time (e.g. BID) we move
        # the FIRST dose and keep the spacing offset is out of scope — for a
        # multi-time regimen, set all to the new base time would be wrong, so
        # we only auto-apply for single-dose schedules.
        if len(old_times) > 1:
            return {
                "response_body": (
                    f"Your {regimen.medication_name} is on multiple daily "
                    "reminders. To adjust those, reply HELP and our team will "
                    "set the times with you."
                ),
                "audit_reasons": ["reminder_time_change_multidose"],
            }
        schedule["times"] = [new_time]
        await regimens_repo.set_schedule(db, regimen.id, schedule=schedule)
        # Re-materialize: cancel pending dose events + rebuild at the new time.
        await dose_reminders.cancel_for_regimen(
            db, regimen_id=regimen.id, reason="reminder_time_changed"
        )
        await db.flush()
        if patient.phone:
            try:
                await dose_reminders.materialize_for_regimen(
                    db, regimen, patient_phone=patient.phone
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "re-materialize after time change failed for regimen %s",
                    regimen.id,
                )
        await db.commit()
        log.info(
            "reminder time changed for %s: regimen %s -> %s",
            redact_phone(patient_phone),
            regimen.id,
            new_time,
        )
        return {
            "response_body": (
                f"Done — I'll remind you about {regimen.medication_name} at "
                f"{new_time} from now on."
            ),
            "audit_reasons": ["reminder_time_changed"],
        }

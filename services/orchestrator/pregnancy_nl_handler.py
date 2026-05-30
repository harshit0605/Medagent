"""Conversational pregnancy intake + data-aware status replies (E5/E6).

Two deterministic (no-LLM) flows for the pregnancy cohort:

  * **NL intake (E5)** — "I'm pregnant, LMP 15 Jan" / "pregnant, last period
    15/01/2026" → parse the LMP date and open a Pregnancy (eager-materialize
    the first reminders, set the cohort flag) without the operator-side form.
  * **Data-aware status (E6)** — "how many weeks am I?", "pregnancy checklist",
    "what's next" → reply with the patient's current gestational week +
    trimester + the next upcoming milestone, pulled from their active
    Pregnancy row. (Previously a generic canned string.)

Both are SAFETY-DEFERRED: a message that also reads as a symptom/side-effect is
left for the triage path. We never diagnose; intake is logistics + status is a
read-back of the clinician-anchored timeline.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timezone

from app.logging_redact import redact_phone

log = logging.getLogger(__name__)


_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

# "pregnant" + an LMP/last-period anchor is the intake trigger.
_PREGNANT_RE = re.compile(r"\bpregnan(?:t|cy)\b", re.IGNORECASE)
_LMP_CONTEXT_RE = re.compile(
    r"\b(lmp|last\s+period|last\s+menstrual|last\s+menses|period\s+was|"
    r"period\s+started|periods?\s+on)\b",
    re.IGNORECASE,
)

# Status-query triggers (E6). Note "what's next" carries an apostrophe.
_STATUS_QUERY_RE = re.compile(
    r"(how\s+many\s+weeks|how\s+far\s+along|which\s+week|what\s+week|"
    r"pregnancy\s+(?:checklist|status|update)|what(?:'?s)?\s+next|"
    r"next\s+(?:scan|visit|appointment|milestone)|my\s+(?:trimester|due\s+date))",
    re.IGNORECASE,
)

# Date forms we accept inside an LMP context.
_DATE_ISO_RE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_DATE_DMY_RE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b")
_DATE_DMON_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)(?:\s+(\d{4}))?\b", re.IGNORECASE
)
_DATE_MOND_RE = re.compile(
    r"\b([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:[,\s]+(\d{4}))?\b", re.IGNORECASE
)


def _clamp_year(y: int | None, *, on: date) -> int:
    """Default a missing year to the most recent past occurrence (an LMP is
    always in the past, within ~10 months)."""
    if y is not None:
        return y + 2000 if y < 100 else y
    return on.year


def parse_lmp_date(text: str, *, on: date | None = None) -> date | None:
    """Extract an LMP date from free text. Tries ISO, D/M/Y, 'DD Mon', and
    'Mon DD'. Returns a plausible past date (within ~11 months) or None.

    A future or implausibly-old date is rejected — an LMP must be in the
    recent past."""
    on = on or datetime.now(timezone.utc).date()
    candidates: list[date] = []

    iso = _DATE_ISO_RE.search(text)
    if iso:
        # A full ISO date is unambiguous — use ONLY it (don't let the DMY
        # fallback sub-match the M-D tail of an ISO string, which would
        # silently "rescue" an implausible YYYY into the current year).
        try:
            candidates.append(
                date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
            )
        except ValueError:
            pass
    else:
        m = _DATE_DMY_RE.search(text)
        if m:
            d, mo = int(m.group(1)), int(m.group(2))
            y = _clamp_year(int(m.group(3)) if m.group(3) else None, on=on)
            try:
                candidates.append(date(y, mo, d))
            except ValueError:
                pass

    m = _DATE_DMON_RE.search(text)
    if m and m.group(2).lower() in _MONTHS:
        d = int(m.group(1))
        mo = _MONTHS[m.group(2).lower()]
        y = _clamp_year(int(m.group(3)) if m.group(3) else None, on=on)
        try:
            candidates.append(date(y, mo, d))
        except ValueError:
            pass

    m = _DATE_MOND_RE.search(text)
    if m and m.group(1).lower() in _MONTHS:
        mo = _MONTHS[m.group(1).lower()]
        d = int(m.group(2))
        y = _clamp_year(int(m.group(3)) if m.group(3) else None, on=on)
        try:
            candidates.append(date(y, mo, d))
        except ValueError:
            pass

    # Pick a plausible LMP: in the past, within 311 days (≈44 weeks).
    for c in candidates:
        if c > on:
            # A year-less date that resolved to the future → roll back a year.
            try:
                c = c.replace(year=c.year - 1)
            except ValueError:
                continue
        age = (on - c).days
        if 0 <= age <= 311:
            return c
    return None


def looks_like_pregnancy_intake(text: str | None) -> bool:
    if not text:
        return False
    return bool(
        _PREGNANT_RE.search(text)
        and _LMP_CONTEXT_RE.search(text)
        and parse_lmp_date(text) is not None
    )


def looks_like_pregnancy_status_query(text: str | None) -> bool:
    return bool(text and _STATUS_QUERY_RE.search(text))


def looks_like_pregnancy_nl(text: str | None) -> bool:
    return looks_like_pregnancy_intake(text) or looks_like_pregnancy_status_query(
        text
    )


async def handle_pregnancy_intake(
    *, patient_phone: str, new_user_text: str
) -> dict | None:
    """Open a pregnancy from a NL message. Returns a delta or None."""
    lmp = parse_lmp_date(new_user_text)
    if lmp is None or not looks_like_pregnancy_intake(new_user_text):
        return None

    from app.db.repositories import patients as patients_repo
    from app.db.repositories import pregnancies as pregnancies_repo
    from app.db.session import get_sessionmaker
    from services.orchestrator import pregnancy as preg_math
    from services.scheduler import pregnancy_milestones

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        patient = await patients_repo.get_by_phone(db, patient_phone)
        if patient is None:
            return None
        existing = await pregnancies_repo.get_active_for_patient(db, patient.id)
        if existing is not None:
            return _status_reply_for(existing, prefix="You're already set up. ")

        edd = preg_math.edd_from_lmp(lmp)
        row = await pregnancies_repo.create(
            db, patient_id=patient.id, lmp_date=lmp, edd=edd
        )
        patient.cohort_pregnancy = True
        await db.flush()
        if patient.phone:
            try:
                await pregnancy_milestones.materialize_for_pregnancy(
                    db, row, patient_phone=patient.phone
                )
            except Exception:  # noqa: BLE001
                log.exception("eager pregnancy materialize failed (NL intake)")
        await db.commit()
        log.info(
            "pregnancy NL intake for %s (LMP %s)",
            redact_phone(patient_phone),
            lmp.isoformat(),
        )
        return _status_reply_for(
            row,
            prefix=(
                "Congratulations — I've set up your pregnancy timeline. "
            ),
            audit=["pregnancy_nl_intake"],
        )


async def handle_pregnancy_status(
    *, patient_phone: str, new_user_text: str
) -> dict | None:
    """Data-aware reply to a pregnancy-status question (E6)."""
    if not looks_like_pregnancy_status_query(new_user_text):
        return None

    from app.db.repositories import patients as patients_repo
    from app.db.repositories import pregnancies as pregnancies_repo
    from app.db.session import get_sessionmaker

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        patient = await patients_repo.get_by_phone(db, patient_phone)
        if patient is None:
            return None
        preg = await pregnancies_repo.get_active_for_patient(db, patient.id)
    if preg is None:
        return {
            "response_body": (
                "I don't have a pregnancy on file for you yet. If you're "
                "pregnant, reply with your last period date (e.g. 'pregnant, "
                "LMP 15 Jan') and I'll set up your timeline."
            ),
            "audit_reasons": ["pregnancy_status_no_record"],
        }
    return _status_reply_for(preg, audit=["pregnancy_status_query"])


def _status_reply_for(
    preg, *, prefix: str = "", audit: list[str] | None = None
) -> dict:
    """Build a current-week + next-milestone reply from a Pregnancy row."""
    from services.orchestrator import pregnancy as preg_math

    on = datetime.now(timezone.utc).date()
    try:
        lmp, _edd = preg_math.resolve_lmp_edd(preg.lmp_date, preg.edd)
    except ValueError:
        lmp = None

    if lmp is None:
        body = prefix + "Your pregnancy is on file."
    else:
        week, _days = preg_math.gestational_age(lmp, on)
        tri = preg_math.trimester(week)
        body = (
            prefix
            + f"You're about {week} weeks along (trimester {tri})."
        )
        nm = preg_math.next_milestone(lmp, on=on)
        if nm is not None:
            milestone, target = nm
            body += (
                f"\n\nNext up: {milestone.title} (around week {milestone.week})"
                f" — {milestone.detail}."
            )
        body += "\n\nReply with any questions, or HELP to reach your care team."
    return {
        "response_body": body,
        "audit_reasons": audit or ["pregnancy_status_query"],
    }


async def handle_pregnancy_nl(
    *, patient_phone: str, new_user_text: str
) -> dict | None:
    """Dispatch a pregnancy NL message: intake first (more specific), then a
    status query. Returns None when neither applies."""
    delta = await handle_pregnancy_intake(
        patient_phone=patient_phone, new_user_text=new_user_text
    )
    if delta is not None:
        return delta
    return await handle_pregnancy_status(
        patient_phone=patient_phone, new_user_text=new_user_text
    )

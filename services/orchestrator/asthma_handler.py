"""Asthma self-report capture: rescue-inhaler usage + trigger diary.

Two inbound flows for the asthma cohort (SoT §3C, V1 "asthma rescue/puff
analytics"), both mirroring the vitals self-report pattern (regex parse →
persist → ack), so they transparently work over text AND transcribed voice:

  - **Rescue-inhaler usage** ("used my reliever 3 times", "2 puffs of
    salbutamol"). Logged as a ``metric_observation`` (metric_key=
    ``rescue_inhaler_puffs``, source=``patient_self_report``). We then check a
    rolling-7-day control threshold — frequent reliever use is the canonical
    signal that asthma isn't well controlled (GINA) — and open an
    ``asthma_control`` ops ticket so a human reviews the controller plan.
  - **Trigger diary** ("trigger: dust", "triggered by exercise"). Logged to
    ``asthma_trigger_logs`` so the clinic can spot patterns.

Design notes:
- **Regex, not LLM.** Short, structured, high-volume, safety-adjacent — same
  rationale as vitals. Rescue parsing is conservative: a bare "took my
  inhaler" (likely a *controller* dose) does NOT count as rescue use; we
  require a rescue-specific word or an explicit usage count.
- **Routing**: ranked AFTER side-effect/symptom detection so "used my inhaler
  5 times, can't breathe" routes to clinical triage first (safety wins).
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from app.logging_redact import redact_phone

log = logging.getLogger(__name__)

RESCUE_METRIC_KEY = "rescue_inhaler_puffs"
# A "refilled my inhaler" / "new inhaler" signal records this so the
# puff-remaining estimate counts only puffs AFTER the latest refill.
RESCUE_REFILL_METRIC_KEY = "rescue_inhaler_refill"
ASTHMA_CONTROL_CATEGORY = "asthma_control"

# Rolling-7-day control thresholds. Either trips a poor-control flag.
_CONTROL_PUFFS_7D = 8  # frequent reliever use (GINA: >2 days/wk OR high count)
_CONTROL_DAYS_7D = 3  # reliever use on 3+ separate days in the week
_MAX_PLAUSIBLE_PUFFS = 40  # a single report above this is a typo, not a count


def _canister_puffs() -> int:
    """Doses in a full rescue-inhaler canister. Default 200 — a standard
    salbutamol MDI. Env-configurable for other devices."""
    try:
        return int(os.getenv("ASTHMA_RESCUE_CANISTER_PUFFS", "200"))
    except ValueError:
        return 200


def _refill_threshold() -> int:
    """Remaining-puff count at/below which we nudge a refill. Default 20
    (≈10% of a 200-dose canister) — enough lead time to reorder."""
    try:
        return int(os.getenv("ASTHMA_RESCUE_REFILL_THRESHOLD", "20"))
    except ValueError:
        return 20


def puffs_remaining(*, canister_size: int, used_since_refill: int) -> int:
    """Estimated rescue puffs left in the current canister. Clamped to
    ``[0, canister_size]`` — over-use past the canister reads as 0, not
    negative (pure; unit-tested)."""
    return max(0, min(canister_size, canister_size - used_since_refill))


# "Refilled my inhaler" / "new inhaler" / "got a new reliever" — resets the
# puff-remaining estimate. Kept narrow so a generic "inhaler" doesn't match.
_RESCUE_REFILL_RE = re.compile(
    r"\b(refill(?:ed)?|replaced?|new|got a new|picked up)\b[^.]*\b"
    r"(inhaler|reliever|rescue|salbutamol|ventolin|asthalin)\b",
    re.IGNORECASE,
)


def looks_like_rescue_refill(text: str | None) -> bool:
    return bool(text and _RESCUE_REFILL_RE.search(text))


# ---- rescue-inhaler usage parsing ------------------------------------------

# Any asthma-inhaler context. ``inhaler`` / ``puff`` are weak (a controller is
# also an inhaler); brand/generic reliever names are strong signals.
_RESCUE_CTX_RE = re.compile(
    r"\b(rescue|reliever|salbutamol|albuterol|levosalbutamol|ventolin|asthalin"
    r"|inhaler|puffs?)\b",
    re.IGNORECASE,
)
_RESCUE_STRONG_RE = re.compile(
    r"\b(rescue|reliever|salbutamol|albuterol|levosalbutamol|ventolin|asthalin"
    r"|puffs?)\b",
    re.IGNORECASE,
)
_USAGE_VERB_RE = re.compile(
    r"\b(used?|using|took|take|taking|needed?|puffed|had to (?:use|take))\b",
    re.IGNORECASE,
)
_NUM_WORDS = {
    "once": 1, "twice": 2, "thrice": 3,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_COUNT_UNIT_RE = re.compile(
    r"\b(\d{1,2})\s*(?:times|puffs?|x|doses?)\b", re.IGNORECASE
)
_NUMWORD_RE = re.compile(
    r"\b(once|twice|thrice|one|two|three|four|five|six|seven|eight|nine|ten)\b",
    re.IGNORECASE,
)
_BARE_NUM_RE = re.compile(r"\b(\d{1,2})\b")


def parse_rescue_use(text: str | None) -> int | None:
    """Extract a rescue-inhaler puff/use count, or ``None``.

    Conservative: requires inhaler context AND a count signal (explicit
    number, number-word, or a strong reliever keyword + usage verb defaulting
    to a single use). A bare "took my inhaler" → ``None`` (likely controller).
    """
    if not text or not _RESCUE_CTX_RE.search(text):
        return None

    count: int | None = None
    m = _COUNT_UNIT_RE.search(text)
    if m:
        count = int(m.group(1))
    if count is None:
        wm = _NUMWORD_RE.search(text)
        if wm:
            count = _NUM_WORDS[wm.group(1).lower()]
    if count is None:
        bm = _BARE_NUM_RE.search(text)
        if bm and _RESCUE_STRONG_RE.search(text):
            count = int(bm.group(1))
    if count is None:
        # No number, but a clear single rescue use ("used my reliever").
        if _RESCUE_STRONG_RE.search(text) and _USAGE_VERB_RE.search(text):
            count = 1
    if count is None or count < 1 or count > _MAX_PLAUSIBLE_PUFFS:
        return None
    return count


def looks_like_rescue_log(text: str | None) -> bool:
    return parse_rescue_use(text) is not None


# ---- trigger diary parsing -------------------------------------------------

_TRIGGER_RE = re.compile(
    r"\b(?:trigger(?:ed)?(?:\s*(?:is|was|by|:|=))?|set\s*off\s*by"
    r"|brought\s*on\s*by)\s+(?P<trig>[a-z0-9 ,&'\-]{2,80})",
    re.IGNORECASE,
)
_TRIGGER_FILLER_RE = re.compile(
    r"\b(today|now|again|this\s+(?:morning|evening|afternoon|week))\b.*$",
    re.IGNORECASE,
)


def parse_trigger(text: str | None) -> str | None:
    """Extract the trigger phrase from a diary message, or ``None``."""
    if not text:
        return None
    m = _TRIGGER_RE.search(text)
    if not m:
        return None
    trig = _TRIGGER_FILLER_RE.sub("", m.group("trig")).strip(" ,.-")
    if not trig:
        return None
    return trig[:100]


def looks_like_trigger_log(text: str | None) -> bool:
    return parse_trigger(text) is not None


def looks_like_asthma_log(text: str | None) -> bool:
    """Cheap router gate — true for a rescue-use, rescue-refill, OR
    trigger-diary message."""
    return (
        looks_like_rescue_log(text)
        or looks_like_rescue_refill(text)
        or looks_like_trigger_log(text)
    )


# ---- handlers --------------------------------------------------------------


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


async def handle_rescue_log(
    *, patient_phone: str, new_user_text: str
) -> dict[str, Any] | None:
    """Parse + persist a rescue-inhaler use, run the rolling-7d control check,
    and (when control looks poor) open an idempotent ops ticket. Returns a
    workflow delta or ``None`` when nothing parses / no patient row."""
    from app.db.repositories import (
        care_plan_goals as goals_repo,
        ops_tickets as ops_tickets_repo,
        patients as patients_repo,
    )
    from app.db.session import get_sessionmaker

    count = parse_rescue_use(new_user_text)
    if count is None:
        return None

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        patient = await patients_repo.get_by_phone(db, patient_phone)
        if patient is None:
            return None

        await goals_repo.record_observation(
            db,
            patient_id=patient.id,
            goal_id=None,
            metric_key=RESCUE_METRIC_KEY,
            value=Decimal(count),
            unit="puffs",
            source="patient_self_report",
        )

        week_ago = datetime.now(timezone.utc) - timedelta(days=7)
        # One fetch serves both the 7-day control window and the
        # since-refill cumulative for the puff-remaining estimate.
        recent = await goals_repo.list_observations_for_patient(
            db, patient.id, metric_key=RESCUE_METRIC_KEY, limit=500
        )
        in_week = [o for o in recent if _aware(o.observed_at) >= week_ago]
        total_puffs = sum(int(o.value) for o in in_week)
        distinct_days = len({_aware(o.observed_at).date() for o in in_week})
        poor_control = (
            total_puffs >= _CONTROL_PUFFS_7D
            or distinct_days >= _CONTROL_DAYS_7D
        )

        # --- puff-based refill estimate (SoT §3C) ---------------------------
        refill_obs = await goals_repo.list_observations_for_patient(
            db, patient.id, metric_key=RESCUE_REFILL_METRIC_KEY, limit=1
        )
        last_refill_at = (
            _aware(refill_obs[0].observed_at) if refill_obs else None
        )
        used_since_refill = sum(
            int(o.value)
            for o in recent
            if last_refill_at is None or _aware(o.observed_at) >= last_refill_at
        )
        remaining = puffs_remaining(
            canister_size=_canister_puffs(),
            used_since_refill=used_since_refill,
        )
        refill_low = remaining <= _refill_threshold()

        if poor_control:
            existing = await ops_tickets_repo.find_open_for_patient_category(
                db, patient_id=patient_phone, category=ASTHMA_CONTROL_CATEGORY
            )
            if existing is None:
                await ops_tickets_repo.create(
                    db,
                    patient_id=patient_phone,
                    category=ASTHMA_CONTROL_CATEGORY,
                    priority="p2",
                    sla_minutes=1440,
                    notes=(
                        f"High reliever use: {total_puffs} puff(s) over "
                        f"{distinct_days} day(s) in the last 7 days. Review "
                        f"controller plan / inhaler technique."
                    ),
                )
        await db.commit()

    puff_word = "puff" if count == 1 else "puffs"
    body = f"Logged your rescue inhaler use ({count} {puff_word})."
    if poor_control:
        body += (
            f"\n\nYou've needed your reliever {total_puffs} time(s) over "
            f"{distinct_days} day(s) this week. Frequent reliever use can mean "
            "your asthma isn't fully controlled — I've flagged this for your "
            "care team to review your controller plan. If you're struggling to "
            "breathe, reply HELP or call now."
        )
    else:
        body += (
            "\n\nKeep tracking — you can also log what set it off "
            "(e.g. 'trigger: dust')."
        )

    audit = ["asthma_rescue_log"]
    if poor_control:
        audit.append("asthma_poor_control")
    # Refill nudge when the estimated remaining puffs run low. Phrased as an
    # estimate (we don't know the exact fill level); patient can correct by
    # logging "refilled my inhaler".
    if refill_low:
        audit.append("asthma_rescue_refill_low")
        body += (
            f"\n\nHeads-up: by my count your rescue inhaler is running low "
            f"(about {remaining} puff(s) left). Consider getting a refill so "
            "you don't run out. Reply 'refilled my inhaler' once you do and "
            "I'll reset the count."
        )
    log.info(
        "asthma rescue use logged for %s: %d puffs (poor_control=%s, "
        "remaining≈%d)",
        redact_phone(patient_phone),
        count,
        poor_control,
        remaining,
    )
    return {"response_body": body, "audit_reasons": audit}


async def handle_trigger_log(
    *, patient_phone: str, new_user_text: str
) -> dict[str, Any] | None:
    """Parse + persist an asthma trigger-diary entry. Returns a workflow delta
    or ``None`` when nothing parses / no patient row."""
    from app.db.repositories import (
        asthma_triggers as triggers_repo,
        patients as patients_repo,
    )
    from app.db.session import get_sessionmaker

    trigger = parse_trigger(new_user_text)
    if trigger is None:
        return None

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        patient = await patients_repo.get_by_phone(db, patient_phone)
        if patient is None:
            return None
        await triggers_repo.create(
            db,
            patient_id=patient.id,
            trigger_text=trigger,
            source="patient_self_report",
        )
        await db.commit()

    body = (
        f"Noted your asthma trigger: {trigger}. I've added it to your trigger "
        "diary — spotting patterns helps your care team adjust your plan."
    )
    # Don't log the trigger free-text (clinical content) at INFO.
    log.info("asthma trigger logged for %s", redact_phone(patient_phone))
    return {"response_body": body, "audit_reasons": ["asthma_trigger_log"]}


async def handle_rescue_refill(
    *, patient_phone: str, new_user_text: str
) -> dict[str, Any] | None:
    """Record a rescue-inhaler refill so the puff-remaining estimate resets.
    Returns a delta or ``None`` when the text isn't a refill signal."""
    if not looks_like_rescue_refill(new_user_text):
        return None

    from app.db.repositories import (
        care_plan_goals as goals_repo,
        patients as patients_repo,
    )
    from app.db.session import get_sessionmaker

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        patient = await patients_repo.get_by_phone(db, patient_phone)
        if patient is None:
            return None
        await goals_repo.record_observation(
            db,
            patient_id=patient.id,
            goal_id=None,
            metric_key=RESCUE_REFILL_METRIC_KEY,
            value=Decimal(_canister_puffs()),
            unit="puffs",
            source="patient_self_report",
        )
        await db.commit()
    log.info("asthma rescue refill recorded for %s", redact_phone(patient_phone))
    return {
        "response_body": (
            "Great — I've reset your rescue inhaler count to a full canister. "
            "I'll let you know when it's running low again."
        ),
        "audit_reasons": ["asthma_rescue_refill"],
    }


async def handle_asthma_log(
    *, patient_phone: str, new_user_text: str
) -> dict[str, Any] | None:
    """Dispatch an asthma self-report. Refill-reset is checked FIRST (most
    specific), then rescue-use, then the trigger diary."""
    delta = await handle_rescue_refill(
        patient_phone=patient_phone, new_user_text=new_user_text
    )
    if delta is not None:
        return delta
    delta = await handle_rescue_log(
        patient_phone=patient_phone, new_user_text=new_user_text
    )
    if delta is not None:
        return delta
    return await handle_trigger_log(
        patient_phone=patient_phone, new_user_text=new_user_text
    )

"""Missed-dose sweep + escalation.

Runs periodically to:
  1. Mark every ``AdherenceEvent`` still ``scheduled`` past its grace window
     as ``missed`` (the patient never tapped Taken / Skipped / Snooze).
  2. For each regimen with a fresh miss, count consecutive misses among the
     most recent N occurrences. When the threshold is hit, auto-create an
     ``ops_ticket`` so a human can follow up — but only if there isn't
     already an open ticket for the same patient + category (avoid spam).

Grace window default: 90 minutes. Matches the dispatcher's freshness
window for ``dose_due``, so a reminder either delivered + got ignored, or
was dropped as stale — either way 90 min past schedule is when the
patient effectively missed it.

Escalation threshold default: 3 consecutive misses on the same regimen.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AdherenceStatus, OpsTicket
from app.db.repositories import adherence_events as adherence_events_repo
from app.db.repositories import caregivers as caregivers_repo
from app.db.repositories import ops_tickets as ops_tickets_repo
from app.db.repositories import patients as patients_repo
from app.db.repositories import regimens as regimens_repo
from app.db.repositories import scheduled_events as scheduled_events_repo
from app.logging_redact import redact_phone

log = logging.getLogger(__name__)


GRACE_WINDOW = timedelta(minutes=90)
ESCALATION_THRESHOLD = 3
ESCALATION_CATEGORY = "missed_doses"
ESCALATION_PRIORITY = "p2"
ESCALATION_SLA_MINUTES = 240  # 4 hours


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def sweep_and_escalate(
    db: AsyncSession,
    *,
    grace_window: timedelta | None = None,
    threshold: int | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """One full pass: mark past-due as missed + escalate over-threshold
    regimens. Idempotent; safe to call repeatedly."""
    grace = grace_window or GRACE_WINDOW
    n_threshold = threshold or ESCALATION_THRESHOLD
    when_now = _ensure_utc(now or datetime.now(timezone.utc))
    cutoff = when_now - grace

    candidates = await adherence_events_repo.list_pending_past(
        db, older_than=cutoff
    )
    marked = 0
    errored = 0
    affected_regimens: set[int] = set()
    for event in candidates:
        # Per-row SAVEPOINT so one bad row (a malformed event, a transient DB
        # error) can't abort the whole pass and starve every other patient's
        # missed-dose marking. The sweep is idempotent, so a skipped row is
        # retried next pass; the savepoint rolls back only that row's work and
        # leaves the session usable for the rest.
        try:
            async with db.begin_nested():
                await adherence_events_repo.mark_missed(
                    db, event.id, at=when_now
                )
            marked += 1
            if event.regimen_id is not None:
                affected_regimens.add(event.regimen_id)
        except Exception:  # noqa: BLE001 — isolate one row, keep sweeping
            log.exception(
                "missed-dose sweep: failed to mark event %s; skipping",
                event.id,
            )
            errored += 1

    escalated = 0
    for regimen_id in affected_regimens:
        try:
            created = None
            async with db.begin_nested():
                if await _should_escalate(
                    db, regimen_id, threshold=n_threshold, up_to=when_now
                ):
                    created = await _open_ticket_for_regimen(db, regimen_id)
            if created is not None:
                escalated += 1
        except Exception:  # noqa: BLE001 — isolate one regimen, keep sweeping
            log.exception(
                "missed-dose sweep: escalation failed for regimen %s; "
                "skipping",
                regimen_id,
            )
            errored += 1

    return {
        "candidates_examined": len(candidates),
        "marked_missed": marked,
        "regimens_checked": len(affected_regimens),
        "escalated": escalated,
        "errored": errored,
    }


async def _should_escalate(
    db: AsyncSession,
    regimen_id: int,
    *,
    threshold: int,
    up_to: datetime,
) -> bool:
    """True iff the most recent ``threshold`` PAST adherence events for this
    regimen are ALL ``missed`` — i.e. consecutive miss streak.

    ``up_to`` excludes future-scheduled occurrences (the materializer
    pre-creates them) which would otherwise outrank actual past misses
    when sorted by ``scheduled_at`` desc."""
    recent = await adherence_events_repo.list_recent_for_regimen(
        db, regimen_id, limit=threshold, up_to=up_to
    )
    if len(recent) < threshold:
        return False
    return all(e.status == AdherenceStatus.missed for e in recent)


async def _open_ticket_for_regimen(
    db: AsyncSession, regimen_id: int
) -> OpsTicket | None:
    """Create an ops_ticket for the patient who owns this regimen, unless
    one is already open for the same patient + missed_doses category."""
    regimen = await regimens_repo.get(db, regimen_id)
    if regimen is None:
        return None
    patient = await patients_repo.get(db, regimen.patient_id)
    if patient is None:
        log.warning(
            "missed-dose escalation: patient %s not found for regimen %s",
            regimen.patient_id,
            regimen_id,
        )
        return None

    existing = await ops_tickets_repo.find_open_for_patient_category(
        db, patient_id=patient.phone, category=ESCALATION_CATEGORY
    )
    if existing is not None:
        log.info(
            "missed-dose escalation skipped: open ticket %s already exists "
            "for patient %s",
            existing.id,
            patient.phone,
        )
        return None

    notes = (
        f"Patient missed {ESCALATION_THRESHOLD} consecutive doses of "
        f"{regimen.medication_name} ({regimen.dose}). Regimen id={regimen_id}, "
        f"patient.id={patient.id}, phone={patient.phone}."
    )
    ticket = await ops_tickets_repo.create(
        db,
        patient_id=patient.phone,
        category=ESCALATION_CATEGORY,
        priority=ESCALATION_PRIORITY,
        sla_minutes=ESCALATION_SLA_MINUTES,
        notes=notes,
    )
    log.info(
        "missed-dose escalation: opened ticket %s for patient %s (regimen %s)",
        ticket.id,
        redact_phone(patient.phone),
        regimen_id,
    )

    # SoT §3B — cardiac (hypertension) patients: a missed-med streak is a
    # caregiver-alert trigger. Notify every confirmed active caregiver so a
    # family member can step in. Best-effort; never blocks the ops ticket.
    if _htn_caregiver_alert_enabled() and getattr(
        patient, "cohort_cardiac", False
    ):
        try:
            await _alert_caregivers_of_streak(db, patient, regimen)
        except Exception:  # noqa: BLE001
            log.exception(
                "HTN caregiver alert enqueue failed for regimen %s", regimen_id
            )
    return ticket


def _htn_caregiver_alert_enabled() -> bool:
    return os.getenv("HTN_CAREGIVER_ALERT_ENABLED", "1") == "1"


async def _alert_caregivers_of_streak(db: AsyncSession, patient, regimen) -> int:
    """Enqueue a ``caregiver_missed_streak`` event per confirmed caregiver.
    The dispatcher renders each into a ``caregiver_missed_streak_v1`` template
    send (caregivers are always out-of-CSW from their own number). Idempotent
    via a per-(patient, regimen, day) idempotency key so the every-tick sweep
    doesn't re-alert the same streak repeatedly."""
    caregivers = await caregivers_repo.list_active_confirmed(db, patient.id)
    if not caregivers:
        return 0
    patient_name = (patient.full_name or "your family member").split(" ")[0]
    med = regimen.medication_name
    day_key = datetime.now(timezone.utc).strftime("%Y%m%d")
    specs = [
        {
            "event_type": "caregiver_missed_streak",
            "patient_id": cg.phone,
            "payload": {
                "caregiver_name": (cg.full_name or "").split(" ")[0],
                "patient_name": patient_name,
                "medication_name": med,
                "patient_db_id": patient.id,
            },
            "idempotency_key": (
                f"cg_streak:{patient.id}:{regimen.id}:{cg.id}:{day_key}"
            ),
            "scheduled_for": datetime.now(timezone.utc),
        }
        for cg in caregivers
    ]
    inserted = await scheduled_events_repo.bulk_enqueue_idempotent(db, specs)
    if inserted:
        log.info(
            "HTN caregiver alert: enqueued %d caregiver notice(s) for "
            "patient %s",
            len(inserted),
            redact_phone(patient.phone),
        )
    return len(inserted)

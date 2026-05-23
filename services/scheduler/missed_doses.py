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
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AdherenceStatus, OpsTicket
from app.db.repositories import adherence_events as adherence_events_repo
from app.db.repositories import ops_tickets as ops_tickets_repo
from app.db.repositories import patients as patients_repo
from app.db.repositories import regimens as regimens_repo

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
    affected_regimens: set[int] = set()
    for event in candidates:
        if event.regimen_id is None:
            # Regimen was deleted; just mark missed for stat correctness.
            await adherence_events_repo.mark_missed(db, event.id, at=when_now)
            marked += 1
            continue
        await adherence_events_repo.mark_missed(db, event.id, at=when_now)
        marked += 1
        affected_regimens.add(event.regimen_id)
    if marked:
        await db.flush()

    escalated = 0
    for regimen_id in affected_regimens:
        if await _should_escalate(
            db, regimen_id, threshold=n_threshold, up_to=when_now
        ):
            created = await _open_ticket_for_regimen(db, regimen_id)
            if created is not None:
                escalated += 1
    if escalated:
        await db.flush()

    return {
        "candidates_examined": len(candidates),
        "marked_missed": marked,
        "regimens_checked": len(affected_regimens),
        "escalated": escalated,
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
        patient.phone,
        regimen_id,
    )
    return ticket

"""Cohort-broadcast orchestration.

Materialises a campaign into per-recipient scheduled events.
The actual WhatsApp send happens via the existing
``services.scheduler.dispatcher`` path — broadcast just enqueues
N events with ``event_type='broadcast_send'`` and lets the
proven retry/consent-gate/delivery-tracking pipeline carry each
one.

Eligibility gates applied at materialisation time:

    consent_sms = False  → skipped, reason=opted_out
    bot_paused_at NOT NULL → skipped, reason=paused
    erased_at NOT NULL  → skipped, reason=erased
    phone column empty   → skipped, reason=no_phone

Why pre-gate AND let the dispatcher gate again? The dispatcher
already checks consent + pause on every send. Pre-gating here
buys two things:

    1. Operator visibility — the campaign detail page shows
       "120 recipients, 5 skipped (opt-out)" up front, before
       the dispatcher tick. Without this they'd see "120
       enqueued" and have to wait + filter.
    2. Audit clarity — the broadcast_sends row records WHY a
       send was skipped at the campaign level. The dispatcher's
       skip is a runtime defense; the broadcast skip is the
       intent capture.

If a patient's eligibility flips between materialisation and
dispatch (rare but real — they STOP between sweep ticks), the
dispatcher gate catches it and the scheduled_event ends with
``skipped`` status. The broadcast_send row stays pending →
dispatched in our DB; the actual delivery state lives on the
scheduled_event.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Patient
from app.db.repositories import (
    broadcast_campaigns as broadcast_campaigns_repo,
)
from app.db.repositories import scheduled_events as scheduled_events_repo

log = logging.getLogger(__name__)


# Cohort-filter shape:
#   {"cohort": "diabetes" | "cardiac" | "fall_risk"}
# Future v2 supports cohort_tag_id + composite filters.
_LEGACY_COHORT_FIELDS: dict[str, str] = {
    "diabetes": "cohort_diabetes",
    "cardiac": "cohort_cardiac",
    "fall_risk": "cohort_fall_risk",
}


async def resolve_recipients(
    db: AsyncSession, *, cohort_filter: dict[str, Any]
) -> list[Patient]:
    """Resolve a cohort filter to the matching patient rows.

    For v1 we support the three legacy boolean cohorts. Patients
    flagged ``erased_at`` are EXCLUDED from the result — the
    erasure flow already wiped their PII; we shouldn't be
    sending them broadcasts.

    Returns Patient ORM rows so the materialiser has direct
    access to phone + db_id + consent flags without a second
    round-trip.
    """
    cohort = cohort_filter.get("cohort")
    if cohort not in _LEGACY_COHORT_FIELDS:
        raise ValueError(
            f"unsupported cohort filter: {cohort!r}. "
            f"v1 supports: {sorted(_LEGACY_COHORT_FIELDS)}."
        )
    column = getattr(Patient, _LEGACY_COHORT_FIELDS[cohort])
    stmt = (
        select(Patient)
        .where(column.is_(True))
        .where(Patient.erased_at.is_(None))
    )
    return list((await db.execute(stmt)).scalars().all())


def _classify_send(
    patient: Patient,
) -> tuple[str, str | None]:
    """Decide whether a recipient is eligible to receive the
    broadcast. Returns ``(status, skip_reason)`` — status is
    ``"pending"`` for eligible patients (will be enqueued) or
    ``"skipped"`` for ineligible ones."""
    if not patient.phone:
        return "skipped", "no_phone"
    if getattr(patient, "erased_at", None) is not None:
        return "skipped", "erased"
    if patient.consent_sms is False:
        return "skipped", "opted_out"
    if getattr(patient, "bot_paused_at", None) is not None:
        return "skipped", "paused"
    return "pending", None


async def materialise_campaign(
    db: AsyncSession,
    *,
    campaign_id: int,
    when: datetime | None = None,
) -> dict[str, Any]:
    """Resolve recipients + create per-recipient broadcast_sends
    rows + enqueue scheduled events for the eligible subset.
    Idempotent contract: caller is responsible for not invoking
    twice (we don't dedupe — the partial state would be
    confusing and a second materialise on the same campaign is
    almost always a bug).

    Returns counters the caller logs / returns to the API:
        {"total_recipients", "sent_count", "skipped_count",
         "skipped_breakdown": {reason: count}}
    """
    when = when or datetime.now(timezone.utc)
    campaign = await broadcast_campaigns_repo.get(db, campaign_id)
    if campaign is None:
        raise ValueError(f"campaign {campaign_id} not found")
    if campaign.status != "draft":
        raise ValueError(
            f"campaign {campaign_id} is not in draft state "
            f"(current: {campaign.status!r}); refusing to materialise"
        )

    patients = await resolve_recipients(
        db, cohort_filter=campaign.cohort_filter
    )

    sent_count = 0
    skipped_count = 0
    skipped_breakdown: dict[str, int] = {}

    for patient in patients:
        status, skip_reason = _classify_send(patient)
        if status == "pending":
            # Enqueue the dispatcher event. Payload carries the
            # template + params so the dispatcher's broadcast
            # branch can render the message without re-reading
            # the campaign row.
            event = await scheduled_events_repo.enqueue(
                db,
                event_type="broadcast_send",
                patient_id=patient.phone,
                payload={
                    "campaign_id": campaign.id,
                    "template_name": campaign.template_name,
                    "template_params": campaign.template_params,
                },
                scheduled_for=when,
            )
            await broadcast_campaigns_repo.add_send(
                db,
                campaign_id=campaign.id,
                patient_id=patient.phone,
                patient_db_id=patient.id,
                status="pending",
                skip_reason=None,
                scheduled_event_id=event.id,
            )
            sent_count += 1
        else:
            await broadcast_campaigns_repo.add_send(
                db,
                campaign_id=campaign.id,
                patient_id=patient.phone or f"erased:{patient.id}",
                patient_db_id=patient.id,
                status="skipped",
                skip_reason=skip_reason,
                scheduled_event_id=None,
            )
            skipped_count += 1
            if skip_reason:
                skipped_breakdown[skip_reason] = (
                    skipped_breakdown.get(skip_reason, 0) + 1
                )

    await broadcast_campaigns_repo.update_after_materialise(
        db,
        campaign.id,
        total_recipients=len(patients),
        sent_count=sent_count,
        skipped_count=skipped_count,
        when=when,
    )

    log.info(
        "broadcast campaign %s materialised: %d enqueued + %d "
        "skipped (%s) of %d total recipients",
        campaign.id,
        sent_count,
        skipped_count,
        skipped_breakdown,
        len(patients),
    )

    return {
        "total_recipients": len(patients),
        "sent_count": sent_count,
        "skipped_count": skipped_count,
        "skipped_breakdown": skipped_breakdown,
    }

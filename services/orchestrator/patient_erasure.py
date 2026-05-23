"""Patient data erasure — GDPR right-of-erasure / DPDP §13.

Companion to the DSAR right-of-access export. Where ``patient_export``
hands the patient a copy of their data, this module obliterates the
PII pieces of that data on demand.

Approach: **anonymization in place**, not hard delete. Most regional
medical-records retention rules (India, UK, EU member states) require
keeping clinical data for years post-care, but they DO require that
the data no longer be tied to an identifiable person. The compromise:

    - Overwrite PII columns in-place irreversibly (full_name,
      phone, external_id on patients; full_name + phone on
      caregivers).
    - Anonymize free-form text on related rows that might
      contain the patient's name, words, or numbers (message log
      contents, ticket notes, inbound classifications).
    - Keep the structural FK skeleton — regimens, adherence,
      lab follow-ups, recaps stay so the medical record is
      preserved (just no longer identifiable).
    - Mark ``patients.erased_at`` so the rest of the system can
      short-circuit (no outbound, no UI surfaces for live-patient
      flows).

The audit_records table is INTENTIONALLY left intact: regulators
require proof-of-erasure, and the audit log is that proof. We add
ONE more audit row recording the erasure event itself.

Idempotency: erasing an already-erased patient is a no-op; the
endpoint returns the existing erased_at without overwriting.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    BroadcastSend,
    CarePlanGoal,
    Caregiver,
    ClinicalAlert,
    InboundClassification,
    MessageLog,
    MetricObservation,
    OpsTicket,
    Patient,
    VisitBrief,
)
from app.db.repositories import audit as audit_repo
from app.db.repositories import patients as patients_repo

log = logging.getLogger(__name__)


# Sentinel string that overwrites PII free-form fields. Distinct
# enough to be searchable in the DB (e.g. "how many erased rows
# do we have?") but generic enough not to leak any structure.
_ERASED_TOKEN = "[erased]"


def _anonymized_phone(patient_id: int) -> str:
    """Generate a unique anonymized replacement for the phone
    column. The DB enforces uniqueness on ``patients.phone`` so
    a static placeholder won't work for multiple erasures.
    Encodes the patient id + a uuid suffix so the row is still
    distinguishable from other erased rows in audit queries
    without leaking the original number."""
    return f"erased:{patient_id}:{uuid.uuid4().hex[:12]}"


async def erase_patient_data(
    db: AsyncSession,
    *,
    patient_id: int,
    actor: str,
    reason: str,
    now: datetime | None = None,
) -> Patient | None:
    """Run the full erasure flow for a patient. Returns the
    anonymized ``Patient`` row, or ``None`` if the patient_id
    doesn't exist.

    Steps (transaction; caller commits):

        1. Patient row: overwrite full_name, phone, external_id;
           clear consents, cohorts, bot-pause, preferred_language;
           stamp erased_at.
        2. Caregivers: overwrite full_name + phone on every linked
           row.
        3. Message log: overwrite the JSON contents on every
           inbound + outbound row keyed to the original phone.
        4. Ops tickets: overwrite notes on every row keyed to the
           original phone.
        5. Inbound classifications: overwrite inbound_text,
           response_text, summary on every row keyed to the
           original phone.
        6. Clinical alerts: overwrite inbound_text,
           clinical_summary, red_flags, resolution_notes;
           rotate patient_phone snapshot.
        7. Visit briefs: overwrite summary + talking_points +
           red_flags + key_metrics (all LLM-derived from PII).
        8. Broadcast sends: rotate the snapshotted phone in
           ``patient_id``.
        9. Metric observations + care plan goals: clear ``notes``
           free-form fields.
        10. Audit record: write the erasure event itself.

    The audit row's ``patient_id`` carries the ANONYMIZED phone
    so a regulator can trace the erasure forward (anonymized id
    → audit row) but cannot trace it backward (original phone
    has been overwritten everywhere).
    """
    when = now or datetime.now(timezone.utc)
    patient = await patients_repo.get(db, patient_id)
    if patient is None:
        return None

    # Idempotency — re-erasing an already-erased patient is a
    # no-op. Don't overwrite the original erased_at timestamp;
    # the FIRST erasure is the legally-relevant moment.
    if patient.erased_at is not None:
        return patient

    original_phone = patient.phone
    new_phone = _anonymized_phone(patient.id)

    # 1. Patient row.
    patient.full_name = _ERASED_TOKEN
    patient.phone = new_phone
    patient.external_id = None
    patient.consent_sms = False
    patient.consent_voice = False
    patient.consent_email = False
    patient.consent_revoked_at = when
    patient.consent_revoked_reason = "patient_erasure"
    patient.cohort_diabetes = False
    patient.cohort_cardiac = False
    patient.cohort_fall_risk = False
    patient.bot_paused_at = when
    patient.bot_paused_reason = "patient_erasure"
    patient.bot_paused_by = actor
    patient.preferred_language = "en"
    patient.erased_at = when

    # 2. Caregivers — anonymize PII fields. Keep the FK so the
    #    structural relationship is preserved for clinical
    #    retention; the names + phones are the actual PII.
    await db.execute(
        update(Caregiver)
        .where(Caregiver.patient_id == patient.id)
        .values(
            full_name=_ERASED_TOKEN,
            phone=_ERASED_TOKEN,
            relationship_to_patient=None,
            active=False,
        )
    )

    # 3. Message log — overwrite the JSON contents. Inbound rows
    #    contain the patient's actual words; outbound rows contain
    #    our reply (which may quote the patient's name from
    #    onboarding). Both blow away.
    await db.execute(
        update(MessageLog)
        .where(MessageLog.patient_id == original_phone)
        .values(
            message={"erased": True, "erased_at": when.isoformat()},
            patient_id=new_phone,
        )
    )

    # 4. Ops tickets keyed to the original phone — anonymize notes
    #    (which carry the side-effect verbatim text + names from
    #    other handlers).
    await db.execute(
        update(OpsTicket)
        .where(OpsTicket.patient_id == original_phone)
        .values(
            notes=_ERASED_TOKEN,
            patient_id=new_phone,
        )
    )

    # 5. Inbound classifications carry inbound_text + summary
    #    + response_text — all PII.
    await db.execute(
        update(InboundClassification)
        .where(
            InboundClassification.patient_phone == original_phone
        )
        .values(
            inbound_text=_ERASED_TOKEN,
            response_text=_ERASED_TOKEN,
            summary=None,
            patient_phone=new_phone,
        )
    )

    # 6. Clinical alerts (slice 10) — verbatim patient quote in
    #    ``inbound_text`` plus LLM-summarised description in
    #    ``clinical_summary`` and the trigger-string list in
    #    ``red_flags`` are all directly quoted PII. We also
    #    rotate the snapshotted phone column. ``status`` and
    #    ``severity`` stay so historical queries ("how many
    #    critical alerts did we raise last quarter?") still
    #    answer post-erasure.
    await db.execute(
        update(ClinicalAlert)
        .where(ClinicalAlert.patient_id == patient.id)
        .values(
            inbound_text=_ERASED_TOKEN,
            clinical_summary=_ERASED_TOKEN,
            red_flags=[],
            resolution_notes=None,
            patient_phone=new_phone,
        )
    )

    # 7. Visit briefs (slice 8) — every prose field is LLM-
    #    generated from the patient's recent activity, so it's
    #    full of PII (drug names + side-effect quotes + patient
    #    interactions). Wipe the prose + lists; keep
    #    ``llm_model``, token counts, and the FKs so the cost
    #    audit / generation history still reads.
    await db.execute(
        update(VisitBrief)
        .where(VisitBrief.patient_id == patient.id)
        .values(
            summary=_ERASED_TOKEN,
            talking_points=[],
            red_flags=[],
            key_metrics={},
        )
    )

    # 8. Broadcast sends (slice 6) — the ``patient_id`` column
    #    here is a phone-string snapshot from materialisation
    #    time. Rotate it to the anonymized phone so the campaign
    #    detail page can still count the row without leaking the
    #    original identifier. ``patient_db_id`` already FK-points
    #    at the (now-anonymized) patient row.
    await db.execute(
        update(BroadcastSend)
        .where(BroadcastSend.patient_db_id == patient.id)
        .values(patient_id=new_phone)
    )

    # 9. Metric observations + care plan goals (slice 14) —
    #    the ``notes`` columns are free-form and may carry the
    #    patient's words ("logged after meal at restaurant"
    #    style). Wipe both. Numeric values + metric_key stay
    #    so anonymized clinical-trend research is still
    #    possible.
    await db.execute(
        update(MetricObservation)
        .where(MetricObservation.patient_id == patient.id)
        .values(notes=None)
    )
    await db.execute(
        update(CarePlanGoal)
        .where(CarePlanGoal.patient_id == patient.id)
        .values(notes=None)
    )

    await db.flush()

    # 6. Audit. Use the new (anonymized) phone as patient_id so
    #    the audit row joins to the post-erasure state. The
    #    details payload captures the actor + reason + the
    #    structural metadata (no PII).
    await audit_repo.log_workflow_summary(
        db,
        patient_id=new_phone,
        outbound_mode=None,
        flow_action="HOLD",
        reason_codes=["patient_erasure"],
        details={
            "actor": actor,
            "reason": reason,
            "patient_db_id": patient.id,
            "erased_at": when.isoformat(),
        },
        logged_at=when,
    )

    log.warning(
        "patient erasure: id=%s actor=%s reason=%r anonymized_phone=%s",
        patient.id,
        actor,
        reason,
        new_phone,
    )

    return patient

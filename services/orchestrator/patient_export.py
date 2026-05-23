"""Patient data export builder — DSAR right-of-access support.

GDPR Art. 15, India DPDP Act §11, and most regional data-protection
frameworks require that on patient request we provide a copy of the
data we hold about them. This module assembles that copy as a
single JSON document the patient (or their authorised
representative) can download.

Scope (v1):

    Core clinical + interaction data the patient has any reasonable
    expectation we hold:

        - patient demographics + consent state
        - caregivers
        - regimens
        - appointments (last 365 days)
        - adherence events (last 365 days)
        - lab follow-ups
        - appointment recaps
        - cohort tags
        - care-plan exemptions
        - side-effect reports they filed

    Out of scope for v1 (covered in a follow-on slice):

        - message log (inbound + outbound) — large + needs careful
          PII review of any audio / image attachments
        - audit log entries — operational metadata, technically not
          patient data
        - opt-out + bot-paused history beyond current state — the
          current state is included; per-event timeline isn't yet
          tracked

Audit:

    Every successful export writes an ``AuditRecord`` of type
    ``"patient_data_export"`` with the actor + the patient_id so
    we can answer "who exported this patient's data and when" if
    a regulator ever asks.

Format:

    A single dict the orchestrator endpoint serialises as JSON.
    All datetimes ISO-8601 UTC. Enum values rendered as their
    ``.value`` strings. The shape is documented inline so
    consumers (the patient's lawyer, a future v2 right-to-erasure
    flow, automated re-import for portability) can rely on it.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Patient
from app.db.repositories import (
    adherence_events as adherence_events_repo,
    appointment_recaps as appointment_recaps_repo,
    appointments as appointments_repo,
    audit as audit_repo,
    care_plan_exemptions as care_plan_exemptions_repo,
    caregivers as caregivers_repo,
    cohort_tags as cohort_tags_repo,
    lab_followups as lab_followups_repo,
    ops_tickets as ops_tickets_repo,
    patients as patients_repo,
    regimens as regimens_repo,
)


# How far back the bounded sections go. Newer regulations (DPDP)
# don't mandate a window — they mandate "data we hold" — but in
# practice 1 year is what most retention policies cover anyway,
# and limiting the document size keeps the export usable.
_DEFAULT_WINDOW_DAYS = 365


def _iso(value: datetime | date | None) -> str | None:
    """Render a datetime / date as an ISO-8601 string, normalising
    naive datetimes to UTC. Returns None for None so consumers can
    distinguish "no value" from "epoch zero"."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return None


def _enum_value(value: Any) -> Any:
    """Render an enum's ``.value`` for JSON, pass-through anything
    else. Centralised so the section builders below stay terse."""
    if value is None:
        return None
    if hasattr(value, "value"):
        return value.value
    return value


# ---- Section builders ------------------------------------------------------


def _patient_section(p: Patient) -> dict[str, Any]:
    return {
        "id": p.id,
        "external_id": p.external_id,
        "full_name": p.full_name,
        "phone": p.phone,
        "preferred_language": p.preferred_language or "en",
        "consents": {
            "sms": p.consent_sms,
            "voice": p.consent_voice,
            "email": p.consent_email,
            "revoked_at": _iso(p.consent_revoked_at),
            "revoked_reason": p.consent_revoked_reason,
        },
        "cohorts": {
            "diabetes": p.cohort_diabetes,
            "cardiac": p.cohort_cardiac,
            "fall_risk": p.cohort_fall_risk,
        },
        "onboarding_step": p.onboarding_step,
        "bot_paused": {
            "at": _iso(getattr(p, "bot_paused_at", None)),
            "reason": getattr(p, "bot_paused_reason", None),
            "by": getattr(p, "bot_paused_by", None),
        },
        "created_at": _iso(p.created_at),
        "updated_at": _iso(p.updated_at),
    }


async def _caregivers_section(
    db: AsyncSession, patient_id: int
) -> list[dict[str, Any]]:
    # ``include_inactive=True`` so soft-deleted caregivers are
    # included — DSAR is "data we hold", and the audit trail of a
    # caregiver who was once linked to the patient counts.
    rows = await caregivers_repo.list_for_patient(
        db, patient_id, include_inactive=True
    )
    return [
        {
            "id": c.id,
            "full_name": c.full_name,
            "phone": c.phone,
            "relationship_to_patient": c.relationship_to_patient,
            "consent_status": _enum_value(c.consent_status),
            "consent_confirmed_at": _iso(c.consent_confirmed_at),
            "notify_on_recap": c.notify_on_recap,
            "active": c.active,
            "created_at": _iso(c.created_at),
        }
        for c in rows
    ]


async def _regimens_section(
    db: AsyncSession, patient_id: int
) -> list[dict[str, Any]]:
    rows = await regimens_repo.list_for_patient(db, patient_id)
    return [
        {
            "id": r.id,
            "medication_name": r.medication_name,
            "dose": r.dose,
            "schedule": r.schedule,
            "strict_timing": r.strict_timing,
            "starts_on": _iso(r.starts_on),
            "ends_on": _iso(r.ends_on),
            "supply_days_initial": r.supply_days_initial,
            "supply_started_on": _iso(r.supply_started_on),
            "created_at": _iso(r.created_at),
        }
        for r in rows
    ]


async def _appointments_section(
    db: AsyncSession, patient_id: int, since: datetime
) -> list[dict[str, Any]]:
    rows = await appointments_repo.list_for_patient(
        db, patient_id, limit=200
    )
    out: list[dict[str, Any]] = []
    for a in rows:
        scheduled = a.scheduled_for
        if scheduled is not None and scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=timezone.utc)
        if scheduled is not None and scheduled < since:
            continue
        out.append(
            {
                "id": a.id,
                "doctor_id": a.doctor_id,
                "scheduled_for": _iso(a.scheduled_for),
                "end_at": _iso(a.end_at),
                "status": _enum_value(a.status),
                "summary": a.summary,
                "source": a.source,
                "calendar_html_link": a.calendar_html_link,
                "created_at": _iso(a.created_at),
            }
        )
    return out


async def _adherence_section(
    db: AsyncSession, patient_id: int, since: datetime
) -> list[dict[str, Any]]:
    rows = await adherence_events_repo.list_for_patient(
        db, patient_id, since=since, limit=2000
    )
    return [
        {
            "id": e.id,
            "regimen_id": e.regimen_id,
            "scheduled_at": _iso(e.scheduled_at),
            "status": _enum_value(e.status),
            "confirmed_at": _iso(e.confirmed_at),
        }
        for e in rows
    ]


async def _lab_followups_section(
    db: AsyncSession, patient_id: int
) -> list[dict[str, Any]]:
    rows = await lab_followups_repo.list_for_patient(
        db, patient_id, limit=200
    )
    return [
        {
            "id": lab.id,
            "test_name": lab.test_name,
            "status": _enum_value(lab.status),
            "due_by": _iso(lab.due_by),
            "notes": lab.notes,
            "booked_at": _iso(lab.booked_at),
            "completed_at": _iso(lab.completed_at),
            "reviewed_at": _iso(lab.reviewed_at),
            "created_at": _iso(lab.created_at),
        }
        for lab in rows
    ]


async def _recaps_section(
    db: AsyncSession, patient_id: int, since: datetime
) -> list[dict[str, Any]]:
    rows = await appointment_recaps_repo.list_for_patient(
        db, patient_id, limit=200
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        created = r.created_at
        if created is not None and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created is not None and created < since:
            continue
        out.append(
            {
                "id": r.id,
                "appointment_id": r.appointment_id,
                "doctor_id": r.doctor_id,
                "status": _enum_value(r.status),
                "generated_text": r.generated_text,
                "doctor_notes": r.doctor_notes,
                "structured_payload": r.structured_payload,
                "sent_at": _iso(r.sent_at),
                "acknowledged_at": _iso(r.acknowledged_at),
                "authored_by": r.authored_by,
                "created_at": _iso(r.created_at),
            }
        )
    return out


async def _cohort_tags_section(
    db: AsyncSession, patient_id: int
) -> list[dict[str, Any]]:
    # Returns ``list[tuple[PatientCohortTag, CohortTag]]`` — the join
    # carries both the assignment and the tag definition. The
    # assignment row has the ``assigned_at`` timestamp + actor; the
    # tag row has the human-readable label + slug.
    rows = await cohort_tags_repo.list_for_patient(db, patient_id)
    out: list[dict[str, Any]] = []
    for assignment, tag in rows:
        out.append(
            {
                "cohort_tag_id": tag.id,
                "label": tag.label,
                "slug": tag.slug,
                "assigned_at": _iso(
                    getattr(assignment, "assigned_at", None)
                    or getattr(assignment, "created_at", None)
                ),
                "assigned_by": getattr(assignment, "assigned_by", None),
            }
        )
    return out


async def _exemptions_section(
    db: AsyncSession, patient_id: int
) -> list[dict[str, Any]]:
    rows = await care_plan_exemptions_repo.list_for_patient(
        db, patient_id
    )
    return [
        {
            "id": ex.id,
            "care_plan_id": ex.care_plan_id,
            "reason": ex.reason,
            "expires_at": _iso(ex.expires_at),
            "revoked_at": _iso(ex.revoked_at),
            "created_by": ex.created_by,
            "created_at": _iso(ex.created_at),
        }
        for ex in rows
    ]


async def _side_effect_reports_section(
    db: AsyncSession, patient_phone: str
) -> list[dict[str, Any]]:
    # Lazy import — ``_extract_reported_text`` lives in
    # ``services.orchestrator.main`` (the FastAPI app), and main
    # imports THIS module to wire up the export endpoint. A
    # top-level import would be circular; the function-level import
    # breaks the cycle. The helper is small + the import is cached
    # after first call so cost is negligible.
    from services.orchestrator.main import _extract_reported_text

    rows = await ops_tickets_repo.list_for_patient_by_category(
        db, patient_phone, "side_effect_report", limit=200
    )
    return [
        {
            "ticket_id": t.id,
            "status": _enum_value(t.status),
            "priority": t.priority,
            "created_at": _iso(t.created_at),
            "acknowledged_at": _iso(t.acknowledged_at),
            "resolved_at": _iso(t.resolved_at),
            "sla_breached_at": _iso(t.sla_breached_at),
            "reported_text": _extract_reported_text(t.notes),
        }
        for t in rows
    ]


# ---- Public entrypoint ----------------------------------------------------


async def build_patient_export(
    db: AsyncSession,
    *,
    patient_id: int,
    actor: str,
    window_days: int = _DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Aggregate the patient's data into a single JSON-serialisable
    dict. Returns ``None`` when the patient row doesn't exist
    (caller maps to a 404).

    Writes an ``AuditRecord`` of type ``"patient_data_export"`` so
    every export is attributable. The audit row is the legal trail
    a regulator would inspect — DO NOT skip it on success or error
    cases that emit data.
    """
    when = now or datetime.now(timezone.utc)
    since = when - timedelta(days=window_days)
    p = await patients_repo.get(db, patient_id)
    if p is None:
        return None

    # Build all sections concurrently could be tempting but the
    # query layer is small + fast; serial keeps the code simple
    # and the diff against the audit log clean.
    document = {
        "schema_version": 1,
        "exported_at": _iso(when),
        "exported_by": actor,
        "window_days": window_days,
        "patient": _patient_section(p),
        "caregivers": await _caregivers_section(db, p.id),
        "regimens": await _regimens_section(db, p.id),
        "appointments": await _appointments_section(db, p.id, since),
        "adherence_events": await _adherence_section(db, p.id, since),
        "lab_followups": await _lab_followups_section(db, p.id),
        "appointment_recaps": await _recaps_section(db, p.id, since),
        "cohort_tags": await _cohort_tags_section(db, p.id),
        "care_plan_exemptions": await _exemptions_section(db, p.id),
        "side_effect_reports": await _side_effect_reports_section(
            db, p.phone or ""
        ),
    }

    # Counters are useful at the top of the document so a reviewer
    # can see scope at a glance without scrolling through arrays.
    document["counts"] = {
        "caregivers": len(document["caregivers"]),
        "regimens": len(document["regimens"]),
        "appointments": len(document["appointments"]),
        "adherence_events": len(document["adherence_events"]),
        "lab_followups": len(document["lab_followups"]),
        "appointment_recaps": len(document["appointment_recaps"]),
        "cohort_tags": len(document["cohort_tags"]),
        "care_plan_exemptions": len(document["care_plan_exemptions"]),
        "side_effect_reports": len(document["side_effect_reports"]),
    }

    # Audit. record_type lives in the wider 64-char column; using
    # the dedicated logger keeps the trace grep-able while staying
    # within column width constraints.
    await audit_repo.log_patient_data_export(
        db,
        patient_id=p.phone or str(p.id),
        actor=actor,
        reason_codes=["dsar_right_of_access"],
        details={
            "patient_db_id": p.id,
            "window_days": window_days,
            "section_counts": document["counts"],
        },
        logged_at=when,
    )

    return document

"""Doctor paging for critical clinical alerts.

When the triage classifier raises a ``critical`` alert, this
module routes a WhatsApp message to a doctor so they see the
patient-safety signal within minutes (rather than next time
they refresh ``/clinical-alerts``).

Doctor selection rule (deliberately simple for v1):

    1. Most recent doctor from this patient's confirmed
       appointments — they own clinical context.
    2. Fallback: any doctor flagged ``is_on_call=True``,
       picking the lowest-id one for determinism so the
       same alert paged twice in a row hits the same doctor
       (ack flow stays clean).
    3. None — no doctor reachable. The pager still stamps
       ``paged_at`` + bumps ``paged_attempts`` so the re-page
       sweep can see the failed attempt and stop hammering.

We do NOT use the existing dispatcher's outbound-blocked gate
(which keys on patient phone) because the recipient is a
DOCTOR. This module talks to the gateway directly.

Re-pages are same-doctor: simpler than building a fanout-with-
escalation system in v1, and matches the SLA shape of "if you
miss the page, the next attempt is also you, until someone
else manually intervenes". Multi-doctor escalation is a v2
slice.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Appointment, AppointmentStatus, ClinicalAlert, Doctor
from app.gateway_auth import gateway_auth_headers
from app.db.repositories import (
    clinical_alerts as clinical_alerts_repo,
    doctors as doctors_repo,
)

log = logging.getLogger(__name__)


PAGE_EVENT_TYPE = "clinical_alert_page"

# Re-page cadence + cap. Tunable via env if a deployment finds
# 5 min too aggressive (or too patient).
REPAGE_AFTER_SECONDS = int(
    os.getenv("CLINICAL_ALERT_REPAGE_AFTER_SECONDS", "300")
)
MAX_PAGE_ATTEMPTS = int(
    os.getenv("CLINICAL_ALERT_MAX_PAGE_ATTEMPTS", "3")
)


async def _pick_recent_appointment_doctor(
    session: AsyncSession, *, patient_id: int
) -> Doctor | None:
    """Most recent confirmed/completed appointment's doctor —
    they have the freshest clinical context for this patient.
    Returns None if the patient has no appointment history."""
    stmt = (
        select(Appointment)
        .where(Appointment.patient_id == patient_id)
        .where(
            Appointment.status.in_(
                [
                    AppointmentStatus.confirmed,
                    AppointmentStatus.completed,
                ]
            )
        )
        .order_by(desc(Appointment.scheduled_for))
        .limit(1)
    )
    appt = (await session.execute(stmt)).scalar_one_or_none()
    if appt is None:
        return None
    return await session.get(Doctor, appt.doctor_id)


async def pick_doctor_for_alert(
    session: AsyncSession,
    alert: ClinicalAlert,
    *,
    escalation_level: int = 0,
) -> Doctor | None:
    """Apply the selection rule. Returns the chosen doctor or
    None when no one's reachable.

    A doctor without a ``phone`` is never returned — there's
    no point picking a paging target we can't actually page.

    Multi-doctor escalation (on-call rota): ``escalation_level`` is the count
    of prior page attempts. On the FIRST page (level 0) we prefer the patient's
    recent-appointment doctor, falling back to the first on-call doctor. On
    each RE-page (level ≥ 1) we escalate through the on-call rota (ordered by
    id), round-robin, EXCLUDING the doctor we last paged — so an unanswered
    critical alert reaches a different person. With a single on-call doctor we
    re-page them (better than going silent).
    """
    on_call = [d for d in await doctors_repo.list_on_call(session) if d.phone]
    on_call.sort(key=lambda d: d.id)

    if escalation_level <= 0:
        primary = await _pick_recent_appointment_doctor(
            session, patient_id=alert.patient_id
        )
        if primary is not None and primary.phone:
            return primary
        return on_call[0] if on_call else None

    if not on_call:
        return None
    # Escalate to a NEW on-call doctor where possible.
    candidates = [d for d in on_call if d.id != alert.paged_doctor_id]
    pool = candidates or on_call
    return pool[(escalation_level - 1) % len(pool)]


def _format_page_body(alert: ClinicalAlert, doctor: Doctor) -> str:
    """Plain-text WhatsApp body for the doctor page. Hard-cap
    each section so the message fits comfortably in one
    bubble. Includes a deep-link to the alert detail in the
    ops console (when ``OPS_CONSOLE_URL`` is set)."""
    base = os.getenv("OPS_CONSOLE_URL", "").rstrip("/")
    deep_link = (
        f"\n\n{base}/clinical-alerts" if base else ""
    )
    red_flags = ", ".join(alert.red_flags or []) or "—"
    summary = (alert.clinical_summary or "")[:240]
    quote = (alert.inbound_text or "")[:200]
    name = doctor.name or "doctor"
    return (
        f"🚨 CRITICAL ALERT — Hi {name},\n"
        f"Patient #{alert.patient_id} ({alert.patient_phone or '—'}) "
        f"may need urgent review.\n\n"
        f"Red flags: {red_flags}\n"
        f"Summary: {summary}\n"
        f'Patient said: "{quote}"'
        f"{deep_link}\n\n"
        f"Reply ACK to acknowledge in the ops console."
    )


async def _post_to_gateway(
    *,
    doctor_phone: str,
    body: str,
    gateway_url: str | None = None,
    timeout: float = 5.0,
) -> str | None:
    """POST a freeform MessageOut to the gateway. Returns
    None on success, an error string on failure (matches the
    dispatcher contract so the caller can mark the event
    failed/skipped consistently)."""
    base = gateway_url or os.getenv(
        "GATEWAY_URL", "http://localhost:8001"
    )
    url = base.rstrip("/") + "/send"
    message: dict[str, Any] = {
        "patient_id": doctor_phone,
        "phone": doctor_phone,
        "body": body,
        "use_template": False,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                url, json=message, headers=gateway_auth_headers()
            )
            response.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning(
            "clinical alert page failed for doctor_phone=%s: %s",
            doctor_phone,
            exc,
        )
        return f"http_error:{type(exc).__name__}:{exc}"
    return None


async def page_alert(
    session: AsyncSession,
    *,
    alert_id: int,
    gateway_url: str | None = None,
) -> dict[str, Any]:
    """Resolve a doctor + send the WhatsApp page + stamp
    audit. Returns a small dict the caller logs / records:

        {"alert_id", "doctor_id", "doctor_phone", "attempts",
         "error"}

    ``error`` is None on success or a short string when:

        - alert not found / not critical / already acked
        - no doctor reachable
        - gateway / network failure

    The audit columns (paged_at, paged_attempts) are stamped
    in EVERY case (including no-doctor and gateway-failure)
    so the re-page sweep can apply its cap. Otherwise a
    permanent "no doctor reachable" state would loop forever.
    """
    alert = await clinical_alerts_repo.get(session, alert_id)
    if alert is None:
        return {
            "alert_id": alert_id,
            "doctor_id": None,
            "doctor_phone": None,
            "attempts": 0,
            "error": "alert_not_found",
        }
    if alert.severity != "critical":
        return {
            "alert_id": alert_id,
            "doctor_id": None,
            "doctor_phone": None,
            "attempts": alert.paged_attempts or 0,
            "error": f"non_critical_severity:{alert.severity}",
        }
    if alert.status != "open":
        return {
            "alert_id": alert_id,
            "doctor_id": None,
            "doctor_phone": None,
            "attempts": alert.paged_attempts or 0,
            "error": f"non_open_status:{alert.status}",
        }
    if (alert.paged_attempts or 0) >= MAX_PAGE_ATTEMPTS:
        return {
            "alert_id": alert_id,
            "doctor_id": alert.paged_doctor_id,
            "doctor_phone": None,
            "attempts": alert.paged_attempts,
            "error": "max_attempts_reached",
        }

    # Capture the pre-increment count BEFORE calling
    # mark_paged. The repo helper mutates the same identity-
    # mapped row that ``alert`` references — reading
    # ``alert.paged_attempts`` after mark_paged would yield
    # the post-increment value and we'd double-count.
    prior_attempts = alert.paged_attempts or 0

    doctor = await pick_doctor_for_alert(
        session, alert, escalation_level=prior_attempts
    )
    if doctor is None or not doctor.phone:
        # Stamp the no-doctor attempt — the cap will eventually
        # stop the re-page sweep from looping. Ops sees the
        # "paged_doctor_id IS NULL" state and acts manually.
        await clinical_alerts_repo.mark_paged(
            session, alert_id, doctor_id=None
        )
        return {
            "alert_id": alert_id,
            "doctor_id": None,
            "doctor_phone": None,
            "attempts": prior_attempts + 1,
            "error": "no_doctor_reachable",
        }

    body = _format_page_body(alert, doctor)
    err = await _post_to_gateway(
        doctor_phone=doctor.phone, body=body, gateway_url=gateway_url
    )
    # Stamp regardless — failed sends still count toward the
    # cap so the sweep doesn't loop on a broken gateway.
    await clinical_alerts_repo.mark_paged(
        session, alert_id, doctor_id=doctor.id
    )
    return {
        "alert_id": alert_id,
        "doctor_id": doctor.id,
        "doctor_phone": doctor.phone,
        "attempts": prior_attempts + 1,
        "error": err,
    }

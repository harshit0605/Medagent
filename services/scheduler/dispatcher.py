"""Maps a ScheduledEvent row to a WhatsApp send (async).

Two flavours of dispatch:

- Template events (``dose_due``, ``refill_due``, ``triage_alert``,
  ``followup_closure``): mapped to an approved WhatsApp template name and
  shipped as a template send. Works outside the 24h customer-service window.
- Freeform events (``appointment_reminder_24h``, ``appointment_reminder_1h``):
  rendered into a plain-text body referencing the doctor + local time.
  Requires the ``db`` parameter so we can resolve the doctor and verify the
  appointment is still confirmed. Only deliverable inside the 24h CSW —
  follow-up work is to add a real reminder template for outside-window sends.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AppointmentStatus, Patient, ScheduledEvent
from app.db.repositories import appointments as appointments_repo
from app.db.repositories import doctors as doctors_repo
from app.db.repositories import patient_inbound as patient_inbound_repo
from app.db.repositories import patients as patients_repo

log = logging.getLogger(__name__)


_TEMPLATE_BY_EVENT: dict[str, str] = {
    # ``dose_due`` and ``refill_due`` have their own dispatcher branches
    # (freeform interactive buttons in CSW, template fallback out). Kept
    # here for downstream consumers that build messages directly via the
    # legacy mapping (e.g. some integration tests).
    "triage_alert": "triage_alert_v1",
    # followup_closure splits by sub-type; resolved at dispatch time
}

_REMINDER_EVENT_TYPES: frozenset[str] = frozenset(
    {"appointment_reminder_24h", "appointment_reminder_1h"}
)
_DOSE_EVENT_TYPES: frozenset[str] = frozenset({"dose_due"})
_REFILL_EVENT_TYPES: frozenset[str] = frozenset({"refill_due"})
_LAB_EVENT_TYPES: frozenset[str] = frozenset({"lab_followup_due"})
_RECAP_NUDGE_EVENT_TYPES: frozenset[str] = frozenset({"recap_ack_nudge"})
_PREGNANCY_MILESTONE_EVENT_TYPES: frozenset[str] = frozenset(
    {"pregnancy_milestone_due"}
)
_PREGNANCY_WEEKLY_EVENT_TYPES: frozenset[str] = frozenset(
    {"pregnancy_weekly_due"}
)
_SUBSTITUTION_EVENT_TYPES: frozenset[str] = frozenset(
    {"order_substitution_request"}
)
_ORDER_RECEIPT_EVENT_TYPES: frozenset[str] = frozenset({"order_receipt"})
_WEEKLY_TREND_EVENT_TYPES: frozenset[str] = frozenset({"weekly_trend_push"})
_POST_OP_EVENT_TYPES: frozenset[str] = frozenset({"post_op_check_due"})

# How late a reminder can be before we drop it as stale. T-1h has a much
# tighter window — a "1 hour before" reminder that arrives 45 minutes after
# the appointment started is worse than no reminder at all. Doses get a
# 90-minute window past schedule before we abandon and let the missed-dose
# sweep mark them missed. Refill reminders are forgiving — a "you're
# running low" prompt 8 hours late still has clinical value.
_FRESHNESS_BY_EVENT: dict[str, timedelta] = {
    "appointment_reminder_24h": timedelta(hours=6),
    "appointment_reminder_1h": timedelta(minutes=30),
    "dose_due": timedelta(minutes=90),
    "refill_due": timedelta(hours=12),
    # Labs are very forgiving — a "schedule your HbA1c" reminder a day
    # late is still useful.
    "lab_followup_due": timedelta(hours=24),
    # An ack-nudge that's a couple of days late is still meaningful —
    # the patient still hasn't acknowledged the underlying recap.
    "recap_ack_nudge": timedelta(hours=48),
    # Pregnancy reminders are forgiving — a "book your anomaly scan" nudge or
    # a weekly check-in a day or two late is still useful.
    "pregnancy_milestone_due": timedelta(hours=48),
    "pregnancy_weekly_due": timedelta(hours=48),
    # A substitution request stays useful for a while — the patient's reorder
    # is blocked until they decide.
    "order_substitution_request": timedelta(hours=48),
    # A delivery receipt is informational — still worth sending if delayed.
    "order_receipt": timedelta(hours=72),
    # A weekly trend nudge is only meaningful within its week.
    "weekly_trend_push": timedelta(hours=36),
    # Post-op checks are forgiving — a wound-check nudge a day late still helps.
    "post_op_check_due": timedelta(hours=48),
}

# Approved Meta template name for outside-CSW APPOINTMENT reminders.
# Defaults to our custom `appointment_reminder_v1` template — 2 params
# (doctor name, scheduled-time phrase) plus 2 quick-reply buttons
# (Cancel / Reschedule). Override via env to point at a different
# approved template; the dispatcher's param/button shape is fixed to
# this contract so a swap requires keeping the same params.
_REMINDER_TEMPLATE_NAME = os.getenv(
    "WHATSAPP_REMINDER_TEMPLATE_NAME", "appointment_reminder_v1"
)
# Approved Meta template name for outside-CSW DOSE reminders.
# Default is ``dose_reminder_v1`` — body-only template, single text
# param (medication + dose), no QUICK_REPLY buttons. The body asks the
# patient to TYPE TAKEN / SKIP, which the LLM compose path understands.
#
# When ``dose_reminder_v2`` (with QUICK_REPLY buttons) lands at Meta,
# ops flips this env var; the dispatcher's ``_v2``-suffix gate then
# auto-injects the dynamic dose_taken / dose_skipped button payloads.
_DOSE_TEMPLATE_NAME = os.getenv(
    "WHATSAPP_DOSE_TEMPLATE_NAME", "dose_reminder_v1"
)
# Approved Meta template name for outside-CSW REFILL reminders. Defaults
# to the legacy `refill_due_v1` (single text param: medication + dose).
_REFILL_TEMPLATE_NAME = os.getenv(
    "WHATSAPP_REFILL_TEMPLATE_NAME", "refill_due_v1"
)
# Approved Meta template name for outside-CSW LAB FOLLOW-UP reminders.
# Defaults to the legacy `lab_closure_update_v1` (single text param: test
# name); registering a richer lab-specific template later is just an env
# override.
_LAB_TEMPLATE_NAME = os.getenv(
    "WHATSAPP_LAB_TEMPLATE_NAME", "lab_closure_update_v1"
)
# Approved Meta template for outside-CSW pregnancy reminders (weekly check-ins
# AND milestone nudges share it). 3 body params: patient name, gestational
# week, and the focus/update line. Override via env to point at a different
# approved template; the param shape (1_name / 2_week / 3_focus) is fixed.
_PREGNANCY_TEMPLATE_NAME = os.getenv(
    "WHATSAPP_PREGNANCY_TEMPLATE_NAME", "pregnancy_weekly_v1"
)
# Approved Meta template for outside-CSW substitution-approval requests.
# 2 body params (original med, proposed substitute) + Approve / Decline
# quick-reply buttons.
_SUBSTITUTION_TEMPLATE_NAME = os.getenv(
    "WHATSAPP_SUBSTITUTION_TEMPLATE_NAME", "substitution_approval_v1"
)
# Approved Meta template for outside-CSW order delivery receipts. 2 body
# params (medication label, order reference).
_ORDER_RECEIPT_TEMPLATE_NAME = os.getenv(
    "WHATSAPP_ORDER_RECEIPT_TEMPLATE_NAME", "order_receipt_v1"
)
# Approved Meta template for outside-CSW weekly trend pushes. 1 body param
# (the multi-line per-metric summary).
_WEEKLY_TREND_TEMPLATE_NAME = os.getenv(
    "WHATSAPP_WEEKLY_TREND_TEMPLATE_NAME", "weekly_trend_v1"
)
# Approved Meta template for outside-CSW post-op checklist reminders. 2 body
# params (post-op day, the checklist item description).
_POST_OP_TEMPLATE_NAME = os.getenv(
    "WHATSAPP_POST_OP_TEMPLATE_NAME", "post_op_check_v1"
)
# Test override: set WHATSAPP_FORCE_REMINDER_TEMPLATE=1 to always use the
# template path (handy for verifying the outside-CSW route while the patient
# is actually in-CSW).
_FORCE_TEMPLATE = os.getenv("WHATSAPP_FORCE_REMINDER_TEMPLATE", "0") == "1"
_CSW_HOURS = 24
# WhatsApp caps interactive replies at 3 buttons. When the refill execute
# layer is enabled, the "Reorder" CTA replaces "Snooze" on refill reminders
# (the two natural responses to a low-supply prompt are "I refilled" and
# "reorder for me"). Off by default so deployments without a fulfillment
# partner keep the legacy Refilled / Snooze / Need help set.
_REORDER_ENABLED = os.getenv("REFILL_REORDER_ENABLED", "0") == "1"


class ReminderNotApplicable(Exception):
    """Raised when a reminder's underlying appointment no longer warrants a send.

    Caller maps this to a ``not_applicable:`` skip — it's not a failure, just
    a reminder we no longer want to deliver (cancelled or missing appointment).
    """


def _stringify_params(payload: dict[str, Any]) -> dict[str, str]:
    return {k: str(v) for k, v in payload.items() if v is not None}


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _build_message_out(event: ScheduledEvent) -> dict[str, Any]:
    payload = dict(event.payload or {})

    if event.event_type == "followup_closure":
        followup_type = payload.get("followup_type", "lab")
        template_name = (
            "appointment_closure_update_v1"
            if followup_type == "appointment"
            else "lab_closure_update_v1"
        )
    elif event.event_type == "broadcast_send":
        # Broadcast events carry their template_name in the
        # payload — ANY approved template can be broadcast,
        # not just the ones in ``_TEMPLATE_BY_EVENT``.
        template_name = payload.get("template_name")
        template_params = dict(payload.get("template_params") or {})
        if not template_name:
            raise ValueError(
                f"broadcast event {event.id} missing template_name"
            )
        body = f"[broadcast] {template_name}"
        return {
            "patient_id": event.patient_id,
            "body": body,
            "use_template": True,
            "template_name": template_name,
            "template_params": _stringify_params(template_params),
        }
    else:
        template_name = _TEMPLATE_BY_EVENT.get(event.event_type)

    if template_name is None:
        raise ValueError(f"no template mapped for event_type={event.event_type}")

    template_params = _stringify_params(payload)
    body = f"[scheduled:{event.event_type}] {template_name}"

    return {
        "patient_id": event.patient_id,
        "body": body,
        "use_template": True,
        "template_name": template_name,
        "template_params": template_params,
    }


async def _build_dose_reminder(
    db: AsyncSession, event: ScheduledEvent
) -> dict[str, Any]:
    """Build the WhatsApp send for a dose_due event.

    In-CSW: freeform interactive button message (Taken / Snoozed / Skipped).
    Out-of-CSW: template send (single body param ``medication + dose``).

    The patient's tap is decoded by the Next.js webhook against our
    ``dose_taken:{adherence_event_id}`` / ``dose_snoozed:`` / ``dose_skipped:``
    convention, then short-circuited deterministically inside the
    orchestrator's dose handler — no LLM round-trip.
    """
    # Local imports keep the dispatcher's import graph small at module load.
    from app.db.models import AdherenceStatus
    from app.db.repositories import adherence_events as adherence_events_repo

    payload = dict(event.payload or {})
    adherence_event_id = payload.get("adherence_event_id")
    medication_name = payload.get("medication_name") or "your medication"
    dose = payload.get("dose") or ""
    if not adherence_event_id:
        raise ValueError(f"dose payload missing adherence_event_id: {payload}")

    adherence = await adherence_events_repo.get(db, int(adherence_event_id))
    if adherence is None:
        raise ReminderNotApplicable(
            f"adherence event {adherence_event_id} not found"
        )
    # Patient already responded (or we already swept it as missed) —
    # don't re-prompt.
    if adherence.status != AdherenceStatus.scheduled:
        raise ReminderNotApplicable(
            f"adherence event {adherence_event_id} is {adherence.status.value}"
        )

    med_label = (
        f"{medication_name} ({dose})" if dose else medication_name
    )
    body = (
        f"Time for your {med_label}. Did you take it? Tap one of the buttons "
        f"below to log it."
    )

    buttons = [
        {
            "id": f"dose_taken:{adherence_event_id}",
            "label": "Taken",
            "action": "dose_taken",
        },
        {
            "id": f"dose_snoozed:{adherence_event_id}",
            "label": "Snooze 30m",
            "action": "dose_snoozed",
        },
        {
            "id": f"dose_skipped:{adherence_event_id}",
            "label": "Skipped",
            "action": "dose_skipped",
        },
    ]

    in_csw = (not _FORCE_TEMPLATE) and await _patient_in_csw(db, event.patient_id)
    if in_csw:
        return {
            "patient_id": event.patient_id,
            "body": body,
            "use_template": False,
            "buttons": buttons,
        }

    log.info(
        "dose reminder %s: out-of-CSW for patient %s — using template %s",
        event.id,
        event.patient_id,
        _DOSE_TEMPLATE_NAME,
    )
    # Template body takes a single parameter (medication + dose). On the
    # v2 template we also inject the dynamic button-id payloads so a tap
    # round-trips through the same dose_taken / dose_skipped pipeline as
    # the in-CSW freeform path. The dropped "snoozed" button is
    # intentional — Meta caps quick_reply buttons at 3 with strict ordering
    # and a 2-button layout (Taken / Skipped) is the highest-value pair.
    out: dict[str, Any] = {
        "patient_id": event.patient_id,
        "body": body,
        "use_template": True,
        "template_name": _DOSE_TEMPLATE_NAME,
        "template_params": {"1_med": med_label},
    }
    # Only inject buttons against the v2 template — v1 rejects them.
    if _DOSE_TEMPLATE_NAME.endswith("_v2"):
        out["buttons"] = [b for b in buttons if b["action"] != "dose_snoozed"]
    return out


async def _build_refill_reminder(
    db: AsyncSession, event: ScheduledEvent
) -> dict[str, Any]:
    """Build the WhatsApp send for a refill_due event.

    In-CSW: freeform interactive button message
        - Refilled (resets the cycle)
        - Snooze 1d (re-prompts tomorrow)
        - Need help (creates an ops ticket for a human to follow up)

    Out-of-CSW: template send (legacy ``refill_due_v1``, single body param
    "{medication} ({dose})"). Once a richer template is approved we can
    swap names via the env override.

    The patient's tap is decoded by the Next.js webhook against
    ``refill_done:{regimen_id}`` / ``refill_snoozed:{regimen_id}`` /
    ``refill_help:{regimen_id}`` and short-circuited deterministically
    inside the orchestrator's refill handler — no LLM round-trip.
    """
    from app.db.repositories import regimens as regimens_repo

    payload = dict(event.payload or {})
    regimen_id = payload.get("regimen_id")
    medication_name = payload.get("medication_name") or "your medication"
    dose = payload.get("dose") or ""
    days_left = payload.get("days_left")
    stage = payload.get("stage", "d?")
    cycle_key = payload.get("cycle_key")
    if not regimen_id:
        raise ValueError(f"refill payload missing regimen_id: {payload}")

    regimen = await regimens_repo.get(db, int(regimen_id))
    if regimen is None:
        raise ReminderNotApplicable(f"regimen {regimen_id} not found")
    if regimen.ends_on is not None and regimen.ends_on < datetime.now(
        timezone.utc
    ).date():
        raise ReminderNotApplicable(f"regimen {regimen_id} ended")
    # Cycle drift: if the regimen's supply_started_on changed since this
    # event was materialized, the patient already refilled — don't re-prompt.
    current_cycle = (
        regimen.supply_started_on.isoformat()
        if regimen.supply_started_on
        else None
    )
    if cycle_key and current_cycle and cycle_key != current_cycle:
        raise ReminderNotApplicable(
            f"regimen {regimen_id} cycle changed ({cycle_key} → {current_cycle})"
        )

    med_label = f"{medication_name} ({dose})" if dose else medication_name

    if isinstance(days_left, int) and days_left > 1:
        body = (
            f"Your {med_label} supply is running low — about {days_left} "
            f"day(s) left. Tap Refilled when you've picked up more."
        )
    elif days_left == 1:
        body = (
            f"Last-day reminder: your {med_label} supply runs out tomorrow. "
            f"Tap Refilled when you've picked up more."
        )
    else:  # 0 / None
        body = (
            f"Heads up — your {med_label} supply runs out today. Tap Refilled "
            f"once you've picked up your refill so we can keep reminders on track."
        )

    buttons = [
        {
            "id": f"refill_done:{regimen_id}",
            "label": "Refilled",
            "action": "refill_done",
        },
    ]
    if _REORDER_ENABLED:
        buttons.append(
            {
                "id": f"refill_reorder:{regimen_id}",
                "label": "Reorder",
                "action": "refill_reorder",
            }
        )
    else:
        buttons.append(
            {
                "id": f"refill_snoozed:{regimen_id}",
                "label": "Snooze 1 day",
                "action": "refill_snoozed",
            }
        )
    buttons.append(
        {
            "id": f"refill_help:{regimen_id}",
            "label": "Need help",
            "action": "refill_help",
        }
    )

    in_csw = (not _FORCE_TEMPLATE) and await _patient_in_csw(db, event.patient_id)
    if in_csw:
        return {
            "patient_id": event.patient_id,
            "body": body,
            "use_template": False,
            "buttons": buttons,
        }

    log.info(
        "refill reminder %s: out-of-CSW for patient %s — using template %s",
        event.id,
        event.patient_id,
        _REFILL_TEMPLATE_NAME,
    )
    return {
        "patient_id": event.patient_id,
        "body": body,
        "use_template": True,
        "template_name": _REFILL_TEMPLATE_NAME,
        "template_params": {"1_med": med_label},
    }


async def _build_lab_followup_reminder(
    db: AsyncSession, event: ScheduledEvent
) -> dict[str, Any]:
    """Build the WhatsApp send for a lab_followup_due event.

    Status-aware copy:
        - status=due  + stage d7  → 'Heads up — your X test is due in a week'
        - status=due  + stage d1  → 'Reminder — your X test is due tomorrow'
        - status=due  + stage overdue → 'Your X test is overdue — please schedule it'
        - status=booked + stage d1     → 'Reminder — your booked X test is tomorrow'
        - status=booked + stage overdue → 'Did you make it to your X test?'
        - status=completed/reviewed → not_applicable (cancelled by handler,
          but defensive check here too in case of races)

    In-CSW: freeform interactive buttons (Booked / Completed / Need help).
    Out-of-CSW: template send.
    """
    from app.db.models import FollowupStatus
    from app.db.repositories import lab_followups as lab_followups_repo

    payload = dict(event.payload or {})
    lab_id = payload.get("lab_followup_id")
    test_name = payload.get("test_name") or "your lab test"
    stage = payload.get("stage", "d?")
    if not lab_id:
        raise ValueError(f"lab payload missing lab_followup_id: {payload}")

    lab = await lab_followups_repo.get(db, int(lab_id))
    if lab is None:
        raise ReminderNotApplicable(f"lab_followup {lab_id} not found")
    if lab.status in (FollowupStatus.completed, FollowupStatus.reviewed):
        raise ReminderNotApplicable(
            f"lab_followup {lab_id} is {lab.status.value} — not prompting"
        )

    is_booked = lab.status == FollowupStatus.booked
    if stage == "d7":
        body = (
            f"Heads up — your {test_name} test is due in about a week. Tap "
            f"Booked once you've scheduled it."
        )
    elif stage == "d1":
        if is_booked:
            body = (
                f"Reminder: your booked {test_name} test is tomorrow. Tap "
                f"Completed once it's done."
            )
        else:
            body = (
                f"Reminder: your {test_name} test is due tomorrow. Tap "
                f"Booked when you've scheduled it, or Completed if you "
                f"already had it done."
            )
    else:  # overdue
        if is_booked:
            body = (
                f"Did you make it to your {test_name} test? Tap Completed "
                f"to log it, or Need help if something came up."
            )
        else:
            body = (
                f"Your {test_name} test is overdue. Tap Booked once you've "
                f"scheduled it, or Need help if you'd like us to assist."
            )

    buttons: list[dict[str, str]] = []
    if not is_booked:
        buttons.append(
            {
                "id": f"lab_booked:{lab_id}",
                "label": "Booked",
                "action": "lab_booked",
            }
        )
    buttons.append(
        {
            "id": f"lab_completed:{lab_id}",
            "label": "Completed",
            "action": "lab_completed",
        }
    )
    buttons.append(
        {
            "id": f"lab_help:{lab_id}",
            "label": "Need help",
            "action": "lab_help",
        }
    )

    in_csw = (not _FORCE_TEMPLATE) and await _patient_in_csw(db, event.patient_id)
    if in_csw:
        return {
            "patient_id": event.patient_id,
            "body": body,
            "use_template": False,
            "buttons": buttons,
        }

    log.info(
        "lab followup %s: out-of-CSW for patient %s — using template %s",
        event.id,
        event.patient_id,
        _LAB_TEMPLATE_NAME,
    )
    return {
        "patient_id": event.patient_id,
        "body": body,
        "use_template": True,
        "template_name": _LAB_TEMPLATE_NAME,
        "template_params": {"1_test": test_name},
    }


async def _patient_first_name(
    db: AsyncSession, *, patient_db_id: Any, patient_phone: str
) -> str:
    """Resolve a friendly first name for pregnancy copy, or 'there'."""
    patient = None
    if patient_db_id is not None:
        patient = await db.get(Patient, int(patient_db_id))
    if patient is None:
        patient = await patients_repo.get_by_phone(db, patient_phone)
    if patient is not None and patient.full_name:
        return patient.full_name.strip().split()[0]
    return "there"


async def _assert_pregnancy_active(db: AsyncSession, pregnancy_id: Any):
    """Fetch the pregnancy and confirm it's still active, else
    ReminderNotApplicable (a delivered/ended pregnancy shouldn't nudge)."""
    from app.db.repositories import pregnancies as pregnancies_repo

    if not pregnancy_id:
        raise ValueError("pregnancy payload missing pregnancy_id")
    pregnancy = await pregnancies_repo.get(db, int(pregnancy_id))
    if pregnancy is None:
        raise ReminderNotApplicable(f"pregnancy {pregnancy_id} not found")
    if pregnancy.status != "active":
        raise ReminderNotApplicable(
            f"pregnancy {pregnancy_id} is {pregnancy.status} — not prompting"
        )
    return pregnancy


async def _build_pregnancy_weekly_reminder(
    db: AsyncSession, event: ScheduledEvent
) -> dict[str, Any]:
    """Build the WhatsApp send for a pregnancy_weekly_due check-in.

    In-CSW: freeform text. Out-of-CSW: ``pregnancy_weekly_v1`` template
    (params: name, gestational week, this-week's focus line).
    """
    payload = dict(event.payload or {})
    await _assert_pregnancy_active(db, payload.get("pregnancy_id"))

    week = payload.get("ga_week")
    focus = payload.get("focus") or ""
    name = await _patient_first_name(
        db,
        patient_db_id=payload.get("patient_db_id"),
        patient_phone=event.patient_id,
    )

    body = (
        f"Hi {name}, you're in week {week} of your pregnancy. {focus}".strip()
    )

    in_csw = (not _FORCE_TEMPLATE) and await _patient_in_csw(db, event.patient_id)
    if in_csw:
        return {
            "patient_id": event.patient_id,
            "body": body,
            "use_template": False,
        }

    log.info(
        "pregnancy weekly %s: out-of-CSW for patient %s — using template %s",
        event.id,
        event.patient_id,
        _PREGNANCY_TEMPLATE_NAME,
    )
    return {
        "patient_id": event.patient_id,
        "body": body,
        "use_template": True,
        "template_name": _PREGNANCY_TEMPLATE_NAME,
        "template_params": {
            "1_name": name,
            "2_week": str(week),
            "3_focus": focus,
        },
    }


async def _build_pregnancy_milestone_reminder(
    db: AsyncSession, event: ScheduledEvent
) -> dict[str, Any]:
    """Build the WhatsApp send for a pregnancy_milestone_due reminder
    (ANC visit / lab / scan / supplement).

    In-CSW: freeform text. Out-of-CSW: the shared ``pregnancy_weekly_v1``
    template, with the milestone rendered into the focus param.
    """
    payload = dict(event.payload or {})
    await _assert_pregnancy_active(db, payload.get("pregnancy_id"))

    week = payload.get("ga_week")
    title = payload.get("title") or "an antenatal milestone"
    detail = payload.get("detail") or ""
    name = await _patient_first_name(
        db,
        patient_db_id=payload.get("patient_db_id"),
        patient_phone=event.patient_id,
    )

    focus = f"{title}: {detail}." if detail else f"{title}."
    body = (
        f"Hi {name}, you're around week {week} — time for {title.lower()}"
        + (f" ({detail})" if detail else "")
        + ". Reply HELP if you'd like us to help arrange it."
    )

    in_csw = (not _FORCE_TEMPLATE) and await _patient_in_csw(db, event.patient_id)
    if in_csw:
        return {
            "patient_id": event.patient_id,
            "body": body,
            "use_template": False,
        }

    log.info(
        "pregnancy milestone %s (%s): out-of-CSW for patient %s — template %s",
        event.id,
        payload.get("milestone_key"),
        event.patient_id,
        _PREGNANCY_TEMPLATE_NAME,
    )
    return {
        "patient_id": event.patient_id,
        "body": body,
        "use_template": True,
        "template_name": _PREGNANCY_TEMPLATE_NAME,
        "template_params": {
            "1_name": name,
            "2_week": str(week),
            "3_focus": focus,
        },
    }


async def _build_substitution_request(
    db: AsyncSession, event: ScheduledEvent
) -> dict[str, Any]:
    """Build the WhatsApp send asking a patient to approve/decline a
    partner-proposed substitution on an order.

    In-CSW: freeform interactive Approve / Decline buttons. Out-of-CSW:
    ``substitution_approval_v1`` template (params: original med, substitute).
    The tap is decoded by the webhook into
    ``[order-action] sub_approve|sub_decline order_id={id}``.
    """
    from app.db.repositories import orders as orders_repo

    payload = dict(event.payload or {})
    order_id = payload.get("order_id")
    if not order_id:
        raise ValueError("substitution payload missing order_id")
    order = await orders_repo.get(db, int(order_id))
    if order is None:
        raise ReminderNotApplicable(f"order {order_id} not found")
    if order.substitution_status != "proposed":
        raise ReminderNotApplicable(
            f"order {order_id} substitution is {order.substitution_status}"
            " — not prompting"
        )

    original = order.medication_name
    substitute = order.substitution_medication or "an equivalent medicine"
    body = (
        f"Your pharmacy is out of {original} and can dispense {substitute} "
        f"instead (same active medicine). Approve this substitution?"
    )
    buttons = [
        {
            "id": f"order_sub_approve:{order_id}",
            "label": "Approve",
            "action": "order_sub_approve",
        },
        {
            "id": f"order_sub_decline:{order_id}",
            "label": "Decline",
            "action": "order_sub_decline",
        },
    ]

    in_csw = (not _FORCE_TEMPLATE) and await _patient_in_csw(db, event.patient_id)
    if in_csw:
        return {
            "patient_id": event.patient_id,
            "body": body,
            "use_template": False,
            "buttons": buttons,
        }

    log.info(
        "substitution request %s: out-of-CSW for patient %s — template %s",
        event.id,
        event.patient_id,
        _SUBSTITUTION_TEMPLATE_NAME,
    )
    return {
        "patient_id": event.patient_id,
        "body": body,
        "use_template": True,
        "template_name": _SUBSTITUTION_TEMPLATE_NAME,
        "template_params": {"1_original": original, "2_substitute": substitute},
    }


async def _build_order_receipt(
    db: AsyncSession, event: ScheduledEvent
) -> dict[str, Any]:
    """Patient-facing delivery receipt for a fulfilled order (MVP #7).

    In-CSW: freeform text. Out-of-CSW: ``order_receipt_v1`` template (med
    label + order reference).
    """
    from app.db.repositories import orders as orders_repo

    payload = dict(event.payload or {})
    order_id = payload.get("order_id")
    if not order_id:
        raise ValueError("order receipt payload missing order_id")
    order = await orders_repo.get(db, int(order_id))
    if order is None:
        raise ReminderNotApplicable(f"order {order_id} not found")

    med_label = (
        f"{order.medication_name} ({order.dose})"
        if order.dose
        else order.medication_name
    )
    order_ref = f"ORD-{order.id}"
    body = (
        f"Delivered: {med_label}. Order ref {order_ref}. Keep this as your "
        f"receipt — reply HELP if anything's wrong with your order."
    )

    in_csw = (not _FORCE_TEMPLATE) and await _patient_in_csw(db, event.patient_id)
    if in_csw:
        return {
            "patient_id": event.patient_id,
            "body": body,
            "use_template": False,
        }
    return {
        "patient_id": event.patient_id,
        "body": body,
        "use_template": True,
        "template_name": _ORDER_RECEIPT_TEMPLATE_NAME,
        "template_params": {"1_med": med_label, "2_ref": order_ref},
    }


async def _build_post_op_check(
    db: AsyncSession, event: ScheduledEvent
) -> dict[str, Any]:
    """Render a post-op checklist reminder. Ends if the episode is closed.

    The wound-photo item invites the patient to reply with a photo (which the
    orchestrator routes to the wound-review queue). In-CSW: freeform; out:
    ``post_op_check_v1`` template (post-op day + item)."""
    from app.db.repositories import post_op as post_op_repo

    payload = dict(event.payload or {})
    episode_id = payload.get("episode_id")
    if not episode_id:
        raise ValueError("post-op payload missing episode_id")
    episode = await post_op_repo.get(db, int(episode_id))
    if episode is None:
        raise ReminderNotApplicable(f"post-op episode {episode_id} not found")
    if episode.status != "active":
        raise ReminderNotApplicable(
            f"post-op episode {episode_id} is {episode.status} — not prompting"
        )

    day = payload.get("post_op_day")
    title = payload.get("title") or "a recovery check"
    detail = payload.get("detail") or ""
    is_wound_photo = payload.get("check_key") == "wound_photo"

    if is_wound_photo:
        body = (
            f"Day {day} after your {episode.procedure_name}: please reply with "
            f"{detail}. If you can't, reply HELP and we'll arrange a check."
        )
    else:
        body = (
            f"Day {day} after your {episode.procedure_name}: time for {title} — "
            f"{detail}. Reply HELP if you have any concerns."
        )

    in_csw = (not _FORCE_TEMPLATE) and await _patient_in_csw(db, event.patient_id)
    if in_csw:
        return {
            "patient_id": event.patient_id,
            "body": body,
            "use_template": False,
        }
    return {
        "patient_id": event.patient_id,
        "body": body,
        "use_template": True,
        "template_name": _POST_OP_TEMPLATE_NAME,
        "template_params": {"1_day": str(day), "2_item": f"{title}: {detail}"},
    }


async def _build_weekly_trend(
    db: AsyncSession, event: ScheduledEvent
) -> dict[str, Any]:
    """Render the weekly self-report trend summary.

    In-CSW: freeform text. Out-of-CSW: ``weekly_trend_v1`` template (single
    body param = the multi-line per-metric summary)."""
    payload = dict(event.payload or {})
    summary = payload.get("summary") or []
    if not summary:
        raise ReminderNotApplicable("weekly trend has no metrics")
    lines = [
        f"• {m.get('label')}: {m.get('count')} reading(s), latest "
        f"{m.get('latest')} {m.get('unit', '')}".rstrip()
        for m in summary
    ]
    detail = "\n".join(lines)
    body = (
        "Your week in numbers:\n"
        f"{detail}\n\n"
        "Keep it up — reply with any new readings, or HELP for support."
    )

    in_csw = (not _FORCE_TEMPLATE) and await _patient_in_csw(db, event.patient_id)
    if in_csw:
        return {
            "patient_id": event.patient_id,
            "body": body,
            "use_template": False,
        }
    return {
        "patient_id": event.patient_id,
        "body": body,
        "use_template": True,
        "template_name": _WEEKLY_TREND_TEMPLATE_NAME,
        "template_params": {"1_summary": detail},
    }


async def _patient_in_csw(db: AsyncSession, patient_phone: str) -> bool:
    """True if the patient has messaged us within the WhatsApp 24h CSW.

    Outside the window, freeform interactive sends (text + buttons + lists)
    are blocked by Meta — we must use a pre-approved template send.
    """
    last = await patient_inbound_repo.get_last_inbound(db, patient_phone)
    if last is None:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last) <= timedelta(hours=_CSW_HOURS)


async def _build_appointment_reminder(
    db: AsyncSession, event: ScheduledEvent
) -> dict[str, Any]:
    payload = dict(event.payload or {})
    appointment_id = payload.get("appointment_id")
    doctor_id = payload.get("doctor_id")
    appt_start_iso = payload.get("appointment_start_iso")
    if not appointment_id or not appt_start_iso:
        raise ValueError(f"reminder payload missing fields: {payload}")

    appointment = await appointments_repo.get(db, int(appointment_id))
    if appointment is None:
        raise ReminderNotApplicable(f"appointment {appointment_id} not found")
    if appointment.status != AppointmentStatus.confirmed:
        raise ReminderNotApplicable(
            f"appointment {appointment_id} is {appointment.status.value}"
        )

    doctor = await doctors_repo.get(db, int(doctor_id)) if doctor_id else None
    doctor_name = doctor.name if doctor else "your doctor"
    tz_name = doctor.timezone if doctor else "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    appt_start = _ensure_utc(datetime.fromisoformat(appt_start_iso))
    when_local = appt_start.astimezone(tz).strftime("%a %d %b at %I:%M %p")
    date_str = appt_start.astimezone(tz).strftime("%a %d %b")
    time_str = appt_start.astimezone(tz).strftime("%I:%M %p") + f" ({tz_name})"

    # ``when_phrase`` is used by the freeform body. The template path uses
    # the more decomposed (date, time) pair below — Meta's library
    # appointment_reminder template has separate {{date}} and {{text}} slots.
    if event.event_type == "appointment_reminder_24h":
        when_phrase = f"tomorrow, {when_local} ({tz_name})"
    else:  # appointment_reminder_1h
        when_phrase = f"in about 1 hour ({when_local} {tz_name})"
    body = (
        f"Reminder: you have an appointment with {doctor_name} {when_phrase}."
    )

    # Same buttons regardless of send mode — only the wire envelope differs:
    #   in-CSW  → freeform interactive button message (label visible to user
    #              from this dict)
    #   out-CSW → template send; gateway extracts button.id as the dynamic
    #              quick_reply payload at the matching index. The button
    #              LABEL comes from the registered template definition; the
    #              `label` field here is unused on the template path but kept
    #              for log/debug clarity.
    buttons = [
        {
            "id": f"cancel_appt:{appointment.id}",
            "label": "Cancel appointment",
            "action": "cancel_appointment",
        },
        {
            "id": f"reschedule_appt:{appointment.id}",
            "label": "Reschedule",
            "action": "reschedule_appointment",
        },
    ]

    in_csw = (not _FORCE_TEMPLATE) and await _patient_in_csw(db, event.patient_id)

    if in_csw:
        return {
            "patient_id": event.patient_id,
            "body": body,
            "use_template": False,
            "buttons": buttons,
        }

    # Outside CSW (or test override): template send.
    log.info(
        "appointment reminder %s: out-of-CSW for patient %s — using template %s",
        event.id,
        event.patient_id,
        _REMINDER_TEMPLATE_NAME,
    )

    # Custom template `appointment_reminder_v1` body:
    #   "Hi from your care team. This is a friendly reminder that you
    #    have an upcoming appointment with {{1}}. Scheduled time: {{2}}.
    #    Please use the buttons below if you need to cancel or
    #    reschedule."
    # Two quick-reply buttons (Cancel / Reschedule) — labels come from
    # the registered template; we inject the dynamic id payloads via
    # ``buttons`` so the patient's tap is decoded by the Next.js webhook
    # back to ``cancel_appt:{id}`` / ``reschedule_appt:{id}``.
    when_phrase_template = (
        f"tomorrow, {when_local} ({tz_name})"
        if event.event_type == "appointment_reminder_24h"
        else f"in about 1 hour ({when_local} {tz_name})"
    )
    template_params = {
        "1_doctor": doctor_name,
        "2_when": when_phrase_template,
    }

    out: dict[str, Any] = {
        "patient_id": event.patient_id,
        "body": body,
        "use_template": True,
        "template_name": _REMINDER_TEMPLATE_NAME,
        "template_params": template_params,
        "buttons": buttons,
    }
    return out


def _is_stale(event: ScheduledEvent, *, now: datetime | None = None) -> timedelta | None:
    """Return how late this event is past its freshness window, else None."""
    max_delay = _FRESHNESS_BY_EVENT.get(event.event_type)
    if max_delay is None:
        return None
    when_now = now or datetime.now(timezone.utc)
    delay = when_now - _ensure_utc(event.scheduled_for)
    return delay if delay > max_delay else None


async def _build_recap_ack_nudge(
    db: AsyncSession, event: ScheduledEvent
) -> dict[str, Any]:
    """Build a one-time gentle WhatsApp nudge to a patient whose recap
    hasn't been acked. Skipped (``ReminderNotApplicable``) when the
    underlying recap has already been acknowledged or questioned in the
    interval since the sweep enqueued this event.

    In-CSW: short freeform "did you see the recap?" with the same Got it /
    I have a question quick replies. Out-of-CSW: same approved recap
    template (``post_visit_recap_v1``) — the patient gets a single re-
    engagement prompt that re-opens the CSW. We don't add a separate
    "ack reminder" template; that doubles the Meta approval surface for
    little benefit."""
    from app.db.models import RecapStatus
    from app.db.repositories import (
        appointment_recaps as appointment_recaps_repo,
    )
    from app.db.repositories import patients as patients_repo

    payload = dict(event.payload or {})
    recap_id = payload.get("recap_id")
    if not recap_id:
        raise ValueError(f"recap_ack_nudge missing recap_id: {payload}")

    recap = await appointment_recaps_repo.get(db, int(recap_id))
    if recap is None:
        raise ReminderNotApplicable(f"recap {recap_id} not found")
    # If the patient acked / asked a question between the sweep enqueueing
    # this event and the dispatcher picking it up, the nudge would be
    # noise. Skip with the standard "not_applicable" so the scheduler
    # marks the event ``skipped`` instead of failed.
    if recap.status != RecapStatus.sent:
        raise ReminderNotApplicable(
            f"recap {recap_id} is {recap.status.value}; nudge unnecessary"
        )

    patient = await patients_repo.get(db, recap.patient_id)
    if patient is None:
        raise ReminderNotApplicable(
            f"patient {recap.patient_id} missing for recap {recap_id}"
        )

    salutation = (
        patient.full_name.split()[0]
        if patient.full_name
        else "there"
    )
    body = (
        f"Hi {salutation}, just checking — did you see the recap from your "
        f"recent visit? Reply OK to confirm, or QUESTION if anything was "
        f"unclear."
    )

    in_csw = (not _FORCE_TEMPLATE) and await _patient_in_csw(
        db, event.patient_id
    )
    if in_csw:
        return {
            "patient_id": event.patient_id,
            "body": body,
            "use_template": False,
            "quick_replies": [
                {"id": f"recap-ack-{recap.id}", "title": "Got it"},
                {
                    "id": f"recap-question-{recap.id}",
                    "title": "I have a question",
                },
            ],
        }

    # Out-of-CSW: re-use the recap template. Param 1 = first name.
    recap_template = os.getenv(
        "WHATSAPP_RECAP_TEMPLATE_NAME", "post_visit_recap_v1"
    )
    return {
        "patient_id": event.patient_id,
        "body": body,
        "use_template": True,
        "template_name": recap_template,
        "template_params": {"1_name": salutation},
    }


async def _patient_outbound_blocked(
    db: AsyncSession, patient_phone: str
) -> str | None:
    """Return a short reason string if outbound is blocked for this
    patient, or ``None`` if the send may proceed. Two distinct
    block sources:

        - ``"opted_out"``: patient sent STOP, ops revoked consent.
                            ``consent_sms`` is False.
        - ``"bot_paused"``: ops-initiated emergency brake. Patient's
                            consent is unchanged but ops has muted
                            the bot for this patient until they
                            unpause.

    Single DB round-trip — both checks come off the same patient
    row. Returns ``None`` on missing patient (defensive — don't
    silently suppress real sends on data gaps)."""
    if not patient_phone:
        return None
    patient = await patients_repo.get_by_phone(db, patient_phone)
    if patient is None:
        return None
    if patient.consent_sms is False:
        return "opted_out"
    if getattr(patient, "bot_paused_at", None) is not None:
        return "bot_paused"
    return None


async def _patient_opted_out(
    db: AsyncSession, patient_phone: str
) -> bool:
    """Backwards-compatible alias kept so older test fixtures still
    work. New code should call ``_patient_outbound_blocked`` directly
    to distinguish the two block sources."""
    return (
        await _patient_outbound_blocked(db, patient_phone)
    ) == "opted_out"


async def _handle_visit_brief_generate(
    event: ScheduledEvent, db: AsyncSession
) -> str | None:
    """Internal-only event handler — invokes the LLM brief
    generator instead of the gateway. Returns ``None`` on
    success, ``unmapped:`` / ``http_error:`` style strings on
    failure so the scheduler's tick loop can mark the event
    appropriately.

    Failed LLM generations are NOT treated as a dispatch
    failure: the generator already persisted a failed-brief
    audit row, and retrying the same event would burn more
    tokens for the same outcome. We log + mark dispatched so
    the event is consumed.
    """
    payload = event.payload or {}
    patient_db_id = payload.get("patient_db_id")
    if not isinstance(patient_db_id, int):
        return "unmapped:visit_brief_generate:missing patient_db_id"
    appointment_id = payload.get("appointment_id")
    doctor_id = payload.get("doctor_id")
    if not isinstance(appointment_id, int):
        appointment_id = None
    if not isinstance(doctor_id, int):
        doctor_id = None

    # Late import to keep dispatcher's import graph clean — the
    # generator imports half the orchestrator and we don't want
    # that load on every scheduler boot when this branch may
    # never fire.
    from services.orchestrator import visit_brief_generator

    try:
        await visit_brief_generator.generate_brief(
            db,
            patient_id=patient_db_id,
            appointment_id=appointment_id,
            doctor_id=doctor_id,
            generated_by="scheduler.auto",
        )
    except RuntimeError as exc:
        # Generator already persisted + committed the failed
        # audit row. Consume the event so we don't loop on it.
        log.warning(
            "visit_brief_generate event %s: %s — marking dispatched",
            event.id,
            exc,
        )
        return None
    except Exception as exc:  # noqa: BLE001 — last-resort safety
        log.exception(
            "visit_brief_generate event %s raised unexpectedly", event.id
        )
        return f"http_error:{type(exc).__name__}:{exc}"
    return None


async def _handle_clinical_alert_page(
    event: ScheduledEvent, db: AsyncSession, gateway_base: str
) -> str | None:
    """Critical-alert paging handler. Calls the pager service
    which resolves a doctor, sends the WhatsApp page, and
    stamps audit columns. Returns None on success or an error
    string on failure.

    Failed pages (gateway errors, no doctor reachable) are
    NOT treated as event failures — the audit row already
    captured the attempt + bumped the counter, and the
    re-page sweep will retry up to ``MAX_PAGE_ATTEMPTS``.
    Returning None marks the event dispatched so we don't
    loop on the same scheduled-events row indefinitely.
    """
    payload = event.payload or {}
    alert_id = payload.get("alert_id")
    if not isinstance(alert_id, int):
        return "unmapped:clinical_alert_page:missing alert_id"

    # Late import — pager pulls in the orchestrator's repo
    # graph and we don't want that on every scheduler boot.
    from services.orchestrator import clinical_alert_pager

    try:
        result = await clinical_alert_pager.page_alert(
            db, alert_id=alert_id, gateway_url=gateway_base
        )
        if result.get("error"):
            log.info(
                "clinical alert page recorded with error: %s",
                result,
            )
        else:
            log.info("clinical alert paged: %s", result)
    except Exception as exc:  # noqa: BLE001 — last-resort safety
        log.exception(
            "clinical_alert_page event %s raised unexpectedly",
            event.id,
        )
        return f"http_error:{type(exc).__name__}:{exc}"
    return None


async def dispatch(
    event: ScheduledEvent,
    *,
    db: AsyncSession | None = None,
    gateway_url: str | None = None,
    timeout: float = 5.0,
) -> str | None:
    """POST a MessageOut for ``event`` to the gateway. Returns ``None`` on
    success or a short error string on failure (network, non-2xx, mapping).

    ``db`` is required for appointment reminder events so we can resolve the
    doctor and verify the appointment is still confirmed. Template events
    don't need it."""
    base = gateway_url or os.getenv("GATEWAY_URL", "http://localhost:8001")

    # Internal-only events that don't produce a WhatsApp send.
    # Handled before the stale check + outbound gate because
    # neither applies — generating a brief on a stale event is
    # still useful (older context > nothing), and there's no
    # patient-facing message to gate on consent for.
    if event.event_type == "visit_brief_generate":
        if db is None:
            return "unmapped:visit_brief_generate"
        return await _handle_visit_brief_generate(event, db)

    # Clinical-alert paging event. Recipient is a DOCTOR, not
    # a patient — the standard outbound-blocked gate keys on
    # patient phone, so we route this through a dedicated
    # handler that talks to the gateway directly with the
    # doctor's phone. We DO honour staleness: if a paging
    # event is far past its scheduled time it's likely the
    # situation is already resolved or escalated through
    # other means, so logging "stale" rather than spamming a
    # doctor is the safer call.
    if event.event_type == "clinical_alert_page":
        if db is None:
            return "unmapped:clinical_alert_page"
        return await _handle_clinical_alert_page(event, db, base)

    stale = _is_stale(event)
    if stale is not None:
        log.warning(
            "dispatch skipped: stale %s by %s", event.event_type, stale
        )
        return f"stale:{event.event_type}:{int(stale.total_seconds())}s"

    # Compliance + ops-safety gate: opted-out patients (patient
    # initiated STOP) AND ops-paused patients (admin emergency
    # brake) both block proactive outbound. Distinct ``not_applicable``
    # sub-prefixes so the scheduler audit + ops dashboard can tell
    # them apart. DB is optional for legacy template events that
    # don't carry one; in that case the gate is a no-op (the
    # gateway-side check is the next line of defence — future v2).
    if db is not None:
        block_reason = await _patient_outbound_blocked(
            db, event.patient_id
        )
        if block_reason is not None:
            log.info(
                "dispatch skipped: patient %s %s (%s)",
                event.patient_id,
                block_reason,
                event.event_type,
            )
            return f"not_applicable:{block_reason}:{event.event_type}"

    try:
        if event.event_type in _REMINDER_EVENT_TYPES:
            if db is None:
                raise ValueError(
                    f"appointment reminder dispatch requires db (event {event.id})"
                )
            message = await _build_appointment_reminder(db, event)
        elif event.event_type in _DOSE_EVENT_TYPES:
            if db is None:
                raise ValueError(
                    f"dose reminder dispatch requires db (event {event.id})"
                )
            message = await _build_dose_reminder(db, event)
        elif event.event_type in _REFILL_EVENT_TYPES:
            if db is None:
                raise ValueError(
                    f"refill reminder dispatch requires db (event {event.id})"
                )
            message = await _build_refill_reminder(db, event)
        elif event.event_type in _LAB_EVENT_TYPES:
            if db is None:
                raise ValueError(
                    f"lab followup dispatch requires db (event {event.id})"
                )
            message = await _build_lab_followup_reminder(db, event)
        elif event.event_type in _RECAP_NUDGE_EVENT_TYPES:
            if db is None:
                raise ValueError(
                    f"recap ack nudge dispatch requires db (event {event.id})"
                )
            message = await _build_recap_ack_nudge(db, event)
        elif event.event_type in _PREGNANCY_MILESTONE_EVENT_TYPES:
            if db is None:
                raise ValueError(
                    f"pregnancy milestone dispatch requires db (event {event.id})"
                )
            message = await _build_pregnancy_milestone_reminder(db, event)
        elif event.event_type in _PREGNANCY_WEEKLY_EVENT_TYPES:
            if db is None:
                raise ValueError(
                    f"pregnancy weekly dispatch requires db (event {event.id})"
                )
            message = await _build_pregnancy_weekly_reminder(db, event)
        elif event.event_type in _SUBSTITUTION_EVENT_TYPES:
            if db is None:
                raise ValueError(
                    f"substitution dispatch requires db (event {event.id})"
                )
            message = await _build_substitution_request(db, event)
        elif event.event_type in _ORDER_RECEIPT_EVENT_TYPES:
            if db is None:
                raise ValueError(
                    f"order receipt dispatch requires db (event {event.id})"
                )
            message = await _build_order_receipt(db, event)
        elif event.event_type in _WEEKLY_TREND_EVENT_TYPES:
            if db is None:
                raise ValueError(
                    f"weekly trend dispatch requires db (event {event.id})"
                )
            message = await _build_weekly_trend(db, event)
        elif event.event_type in _POST_OP_EVENT_TYPES:
            if db is None:
                raise ValueError(
                    f"post-op dispatch requires db (event {event.id})"
                )
            message = await _build_post_op_check(db, event)
        else:
            message = _build_message_out(event)
    except ReminderNotApplicable as exc:
        log.info("dispatch skipped: %s", exc)
        return f"not_applicable:{exc}"
    except ValueError as exc:
        log.warning("dispatch skipped: %s", exc)
        return f"unmapped:{event.event_type}"

    url = base.rstrip("/") + "/send"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=message)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("dispatch failed for event %s: %s", event.id, exc)
        return f"http_error:{type(exc).__name__}:{exc}"
    return None

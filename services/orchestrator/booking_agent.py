"""WhatsApp-driven booking agent — multi-turn ReAct loop using OpenAI tool calls.

Patient says "I want to book with Dr Harshit tomorrow morning" → this module
runs an LLM with three tools, executes whichever tool the model picks,
appends the result to the message history, and loops until the model emits
a final reply.

Tools:
    - list_connected_doctors  : id, name, timezone of doctors with active OAuth
    - find_slots              : free 30-min slots in a window for a doctor
    - book_slot               : create the calendar event + appointments row

State management:
    - The compiled graph's `messages` field accumulates the full conversation
      across turns (per-patient thread_id), so subsequent turns inherit the
      tool calls + tool results from earlier turns.
    - `current_flow="booking"` is set on entry; cleared on successful
      `book_slot`, on patient saying "cancel"/"never mind", or after the
      max-step safety cap.
    - `flow_state` carries small structured data the next turn needs
      (e.g. last set of proposed slots, the chosen doctor_id).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AppointmentStatus, DoctorOAuthStatus
from app.db.repositories import appointments as appointments_repo
from app.db.repositories import doctors as doctors_repo
from app.db.session import get_sessionmaker
from services.orchestrator import google_calendar as gcal
from services.orchestrator.llm import get_llm
from services.scheduler import appointment_reminders, visit_brief_scheduling

log = logging.getLogger(__name__)


MAX_TOOL_STEPS = 6


# ---- Tool schemas (OpenAI function-calling format) -------------------------


_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_connected_doctors",
            "description": (
                "Return the doctors who currently have a connected Google "
                "Calendar (oauth_status='connected'). Call this first when "
                "the patient hasn't named a doctor yet."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_slots",
            "description": (
                "Find free slots on a doctor's calendar inside a time window. "
                "Use this to enumerate options for the patient. Always call "
                "this before book_slot or reschedule_appointment — never "
                "invent a free time. Times must be ISO-8601 with timezone offset."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "doctor_id": {"type": "integer", "description": "Doctor.id"},
                    "start": {
                        "type": "string",
                        "description": "Window start, ISO-8601 with offset (e.g. 2026-05-02T09:00:00+05:30).",
                    },
                    "end": {
                        "type": "string",
                        "description": "Window end, ISO-8601 with offset.",
                    },
                    "duration_minutes": {
                        "type": "integer",
                        "description": "Slot length. Default 30.",
                        "default": 30,
                    },
                },
                "required": ["doctor_id", "start", "end"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_slot",
            "description": (
                "Confirm a booking by creating a calendar event AND a row in "
                "our appointments table. Only call after the patient has "
                "confirmed the doctor + exact start time. Returns the new "
                "appointment ID and a calendar link."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "doctor_id": {"type": "integer"},
                    "start": {
                        "type": "string",
                        "description": "Slot start, ISO-8601 with offset.",
                    },
                    "end": {
                        "type": "string",
                        "description": "Slot end, ISO-8601 with offset.",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Calendar event title (e.g. 'Patient consultation — 918340858764').",
                    },
                    "patient_db_id": {
                        "type": "integer",
                        "description": "Patient.id from our DB. The agent receives this in the system prompt.",
                    },
                    "patient_phone": {
                        "type": "string",
                        "description": "Patient's WhatsApp wa_id (digits only).",
                    },
                },
                "required": ["doctor_id", "start", "end", "summary", "patient_db_id", "patient_phone"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_my_appointments",
            "description": (
                "List the patient's upcoming confirmed appointments. Returns "
                "id, doctor_id, doctor_name, scheduled_for (local time), "
                "calendar_link. Call this when the patient asks about their "
                "bookings, wants to cancel, or wants to reschedule."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "patient_db_id": {
                        "type": "integer",
                        "description": "Patient.id from our DB (in this prompt's header).",
                    }
                },
                "required": ["patient_db_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_appointment",
            "description": (
                "Cancel an existing appointment: deletes the Google Calendar "
                "event AND marks the appointments row as cancelled. The "
                "`appointment_id` MUST be the `appointment_id` value from a "
                "list_my_appointments result — NOT the position number you "
                "showed the patient. The tool verifies ownership against "
                "`patient_db_id` and refuses cross-patient cancellations."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id": {
                        "type": "integer",
                        "description": "Exact `appointment_id` from list_my_appointments — NOT a list index.",
                    },
                    "patient_db_id": {
                        "type": "integer",
                        "description": "Patient.id from this prompt's header.",
                    },
                },
                "required": ["appointment_id", "patient_db_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reschedule_appointment",
            "description": (
                "Move an existing appointment to a new time. Updates the "
                "Google Calendar event AND the appointments row. Always call "
                "find_slots first against the SAME doctor as the appointment, "
                "then call this with one of those slots. The "
                "`appointment_id` MUST be the `appointment_id` value from "
                "list_my_appointments — NOT the position number. The tool "
                "verifies ownership and refuses cross-patient reschedules."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id": {
                        "type": "integer",
                        "description": "Exact `appointment_id` from list_my_appointments — NOT a list index.",
                    },
                    "patient_db_id": {
                        "type": "integer",
                        "description": "Patient.id from this prompt's header.",
                    },
                    "new_start": {
                        "type": "string",
                        "description": "New start, ISO-8601 with offset.",
                    },
                    "new_end": {
                        "type": "string",
                        "description": "New end, ISO-8601 with offset.",
                    },
                },
                "required": ["appointment_id", "patient_db_id", "new_start", "new_end"],
                "additionalProperties": False,
            },
        },
    },
]


# ---- Tool implementations --------------------------------------------------


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _fmt_local(dt: datetime, tz_name: str) -> str:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    return dt.astimezone(tz).strftime("%a %d %b, %I:%M %p")


async def _tool_list_connected_doctors(db: AsyncSession, _args: dict[str, Any]) -> str:
    rows = await doctors_repo.list_all(db)
    connected = [r for r in rows if r.oauth_status == DoctorOAuthStatus.connected]
    if not connected:
        return "No doctors are connected. Tell the patient bookings are unavailable right now."
    return json.dumps(
        [
            {"id": r.id, "name": r.name, "timezone": r.timezone, "calendar_id": r.calendar_id}
            for r in connected
        ]
    )


async def _tool_find_slots(db: AsyncSession, args: dict[str, Any]) -> str:
    try:
        doctor_id = int(args["doctor_id"])
        window = gcal.TimeSlot(
            start=_parse_iso(args["start"]),
            end=_parse_iso(args["end"]),
        )
        duration = int(args.get("duration_minutes", 30))
    except (KeyError, ValueError, TypeError) as exc:
        return f"INVALID_ARGS: {exc}"

    try:
        result = await gcal.find_slots(
            db, doctor_id=doctor_id, window=window, duration_minutes=duration
        )
    except (LookupError, PermissionError) as exc:
        return f"ERROR: {exc}"
    except Exception as exc:  # noqa: BLE001
        log.exception("find_slots failed")
        return f"ERROR: {type(exc).__name__}: {exc}"

    if not result.free:
        return json.dumps(
            {
                "doctor_id": doctor_id,
                "timezone": result.timezone,
                "duration_minutes": duration,
                "free": [],
                "note": "no free slots in this window — propose a different window.",
            }
        )

    # Limit to first 5 to keep the LLM message small + the patient choices manageable.
    slots = result.free[:5]
    return json.dumps(
        {
            "doctor_id": doctor_id,
            "timezone": result.timezone,
            "duration_minutes": duration,
            "free": [
                {
                    "start": s.start.isoformat(),
                    "end": s.end.isoformat(),
                    "local": _fmt_local(s.start, result.timezone),
                }
                for s in slots
            ],
        }
    )


async def _tool_book_slot(db: AsyncSession, args: dict[str, Any]) -> str:
    try:
        doctor_id = int(args["doctor_id"])
        start = _parse_iso(args["start"])
        end = _parse_iso(args["end"])
        summary = str(args["summary"])
        patient_db_id = int(args["patient_db_id"])
        patient_phone = str(args["patient_phone"])
    except (KeyError, ValueError, TypeError) as exc:
        return f"INVALID_ARGS: {exc}"

    # Defensive: refuse to create a duplicate booking when the patient already
    # has a confirmed appointment with this doctor at (effectively) this time.
    # Catches the LLM-drift case where Flow C (reschedule) misroutes into a
    # book_slot call — instead of silently creating a parallel appointment we
    # tell the LLM to use reschedule_appointment against the existing id.
    existing = await appointments_repo.list_for_patient(
        db, patient_db_id, upcoming_only=True, limit=20
    )
    for appt in existing:
        if (
            appt.doctor_id == doctor_id
            and appt.status == AppointmentStatus.confirmed
            and abs((appt.scheduled_for - start).total_seconds()) < 300
        ):
            return (
                f"ERROR: patient already has confirmed appointment "
                f"{appt.id} with doctor {doctor_id} at {appt.scheduled_for.isoformat()}. "
                f"To MOVE that booking, call reschedule_appointment with "
                f"appointment_id={appt.id} and the new start/end. Do not call "
                f"book_slot — that would create a duplicate."
            )

    try:
        event = await gcal.book_slot(
            db,
            doctor_id=doctor_id,
            start=start,
            end=end,
            summary=summary,
            patient_phone=patient_phone,
        )
    except (LookupError, PermissionError) as exc:
        return f"ERROR: {exc}"
    except Exception as exc:  # noqa: BLE001
        log.exception("book_slot failed")
        return f"ERROR: {type(exc).__name__}: {exc}"

    appointment = await appointments_repo.create_confirmed(
        db,
        patient_id=patient_db_id,
        doctor_id=doctor_id,
        scheduled_for=start,
        end_at=end,
        calendar_event_id=event.event_id,
        calendar_html_link=event.html_link,
        summary=summary,
        source="whatsapp_booking_agent",
    )
    # Best-effort reminder enqueue — never fails the booking if scheduling
    # the reminders blows up. The patient already has the calendar invite.
    try:
        await appointment_reminders.materialize_for_appointment(
            db, appointment, patient_phone=patient_phone
        )
    except Exception:  # noqa: BLE001
        log.exception("materialize_for_appointment failed for appt=%s", appointment.id)
    # Auto-schedule the visit brief for T-2h. Same best-effort
    # contract — booking succeeds even if the brief fails to
    # schedule, since the operator can still trigger one
    # manually from the patient detail page.
    try:
        await visit_brief_scheduling.materialize_for_appointment(
            db, appointment, patient_phone=patient_phone
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "visit_brief_scheduling.materialize failed for appt=%s",
            appointment.id,
        )
    await db.commit()

    return json.dumps(
        {
            "ok": True,
            "appointment_id": appointment.id,
            "calendar_event_id": event.event_id,
            "html_link": event.html_link,
            "start": start.isoformat(),
            "end": end.isoformat(),
        }
    )


async def _tool_list_my_appointments(db: AsyncSession, args: dict[str, Any]) -> str:
    try:
        patient_db_id = int(args["patient_db_id"])
    except (KeyError, ValueError, TypeError) as exc:
        return f"INVALID_ARGS: {exc}"

    rows = await appointments_repo.list_for_patient(
        db, patient_db_id, upcoming_only=True, limit=10
    )
    if not rows:
        return json.dumps({"appointments": [], "note": "no upcoming appointments"})

    # Resolve doctor names + timezones for nicer presentation.
    doctor_cache: dict[int, dict[str, Any]] = {}
    for r in rows:
        if r.doctor_id not in doctor_cache:
            doc = await doctors_repo.get(db, r.doctor_id)
            doctor_cache[r.doctor_id] = (
                {"name": doc.name, "timezone": doc.timezone}
                if doc
                else {"name": f"Doctor #{r.doctor_id}", "timezone": "UTC"}
            )

    # Skip cancelled appointments — they shouldn't show up as "upcoming".
    rows = [r for r in rows if r.status == AppointmentStatus.confirmed]
    if not rows:
        return json.dumps({"appointments": [], "note": "no upcoming confirmed appointments"})

    out = []
    for r in rows:
        info = doctor_cache.get(r.doctor_id, {"name": "?", "timezone": "UTC"})
        out.append(
            {
                "appointment_id": r.id,
                "doctor_id": r.doctor_id,
                "doctor_name": info["name"],
                "scheduled_for": r.scheduled_for.isoformat(),
                "scheduled_for_local": _fmt_local(r.scheduled_for, info["timezone"]),
                "end_at": r.end_at.isoformat(),
                "status": r.status.value,
                "calendar_html_link": r.calendar_html_link,
            }
        )
    return json.dumps(
        {
            "appointments": out,
            "_NOTE_FOR_AGENT": (
                "When the patient picks the Nth option from the list you SHOW them, "
                "the appointment_id you must pass to cancel/reschedule is "
                "appointments[N-1].appointment_id — NOT N itself."
            ),
        }
    )


async def _tool_cancel_appointment(db: AsyncSession, args: dict[str, Any]) -> str:
    try:
        appointment_id = int(args["appointment_id"])
        patient_db_id = int(args["patient_db_id"])
    except (KeyError, ValueError, TypeError) as exc:
        return f"INVALID_ARGS: {exc}"

    appt = await appointments_repo.get(db, appointment_id)
    if appt is None:
        return f"ERROR: appointment {appointment_id} not found"
    # SECURITY: refuse cross-patient cancellations. The LLM has been observed
    # to confuse a list-position number with the appointment_id and try to
    # operate on someone else's row.
    if appt.patient_id != patient_db_id:
        return (
            f"ERROR: appointment {appointment_id} does not belong to patient "
            f"{patient_db_id} (it belongs to {appt.patient_id}). Re-check the "
            f"appointment_id from list_my_appointments."
        )
    if appt.status != AppointmentStatus.confirmed:
        return f"ERROR: appointment {appointment_id} is {appt.status.value}, can't cancel"

    if appt.calendar_event_id:
        try:
            await gcal.cancel_event(
                db, doctor_id=appt.doctor_id, event_id=appt.calendar_event_id
            )
        except (LookupError, PermissionError) as exc:
            return f"ERROR: {exc}"
        except Exception as exc:  # noqa: BLE001
            log.exception("calendar cancel failed")
            return f"ERROR: {type(exc).__name__}: {exc}"

    cancelled = await appointments_repo.mark_cancelled(db, appointment_id)
    try:
        await appointment_reminders.cancel_for_appointment(
            db, appointment_id=appointment_id, reason="appointment_cancelled"
        )
    except Exception:  # noqa: BLE001
        log.exception("cancel_for_appointment failed for appt=%s", appointment_id)
    try:
        await visit_brief_scheduling.cancel_for_appointment(
            db, appointment_id=appointment_id, reason="appointment_cancelled"
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "visit_brief_scheduling.cancel failed for appt=%s",
            appointment_id,
        )
    await db.commit()
    return json.dumps(
        {
            "ok": True,
            "appointment_id": cancelled.id if cancelled else appointment_id,
            "status": "cancelled",
        }
    )


async def _tool_reschedule_appointment(db: AsyncSession, args: dict[str, Any]) -> str:
    try:
        appointment_id = int(args["appointment_id"])
        patient_db_id = int(args["patient_db_id"])
        new_start = _parse_iso(args["new_start"])
        new_end = _parse_iso(args["new_end"])
    except (KeyError, ValueError, TypeError) as exc:
        return f"INVALID_ARGS: {exc}"

    appt = await appointments_repo.get(db, appointment_id)
    if appt is None:
        return f"ERROR: appointment {appointment_id} not found"
    # Same ownership check as cancel.
    if appt.patient_id != patient_db_id:
        return (
            f"ERROR: appointment {appointment_id} does not belong to patient "
            f"{patient_db_id} (it belongs to {appt.patient_id}). Re-check the "
            f"appointment_id from list_my_appointments."
        )
    if appt.status != AppointmentStatus.confirmed:
        return f"ERROR: appointment {appointment_id} is {appt.status.value}, can't reschedule"
    if not appt.calendar_event_id:
        return f"ERROR: appointment {appointment_id} has no calendar_event_id"

    try:
        await gcal.reschedule_event(
            db,
            doctor_id=appt.doctor_id,
            event_id=appt.calendar_event_id,
            start=new_start,
            end=new_end,
        )
    except (LookupError, PermissionError) as exc:
        return f"ERROR: {exc}"
    except Exception as exc:  # noqa: BLE001
        log.exception("calendar reschedule failed")
        return f"ERROR: {type(exc).__name__}: {exc}"

    updated = await appointments_repo.reschedule(
        db, appointment_id, new_start=new_start, new_end=new_end
    )
    # Reschedule = old reminders no longer apply, re-materialize against the
    # new time. Cancel first so we don't double-fire if the new time happens
    # to be far enough out that both reminders still slot in.
    try:
        await appointment_reminders.cancel_for_appointment(
            db, appointment_id=appointment_id, reason="appointment_rescheduled"
        )
        if updated is not None:
            await appointment_reminders.materialize_for_appointment(db, updated)
    except Exception:  # noqa: BLE001
        log.exception("reminder reschedule failed for appt=%s", appointment_id)
    # Same logic for the visit-brief generation event — old
    # one is invalidated, new one fires T-2h before the new
    # start. Pending briefs already PERSISTED for the old
    # appointment_id stay (they're audit), but newly-scheduled
    # ones supersede on next gen anyway.
    try:
        await visit_brief_scheduling.cancel_for_appointment(
            db,
            appointment_id=appointment_id,
            reason="appointment_rescheduled",
        )
        if updated is not None:
            await visit_brief_scheduling.materialize_for_appointment(
                db, updated
            )
    except Exception:  # noqa: BLE001
        log.exception(
            "visit_brief_scheduling reschedule failed for appt=%s",
            appointment_id,
        )
    await db.commit()
    return json.dumps(
        {
            "ok": True,
            "appointment_id": appointment_id,
            "new_start": new_start.isoformat(),
            "new_end": new_end.isoformat(),
            "html_link": updated.calendar_html_link if updated else None,
        }
    )


_TOOL_DISPATCH = {
    "list_connected_doctors": _tool_list_connected_doctors,
    "find_slots": _tool_find_slots,
    "book_slot": _tool_book_slot,
    "list_my_appointments": _tool_list_my_appointments,
    "cancel_appointment": _tool_cancel_appointment,
    "reschedule_appointment": _tool_reschedule_appointment,
}


# ---- ReAct loop -------------------------------------------------------------


def _system_prompt(*, patient_db_id: int | None, patient_phone: str) -> str:
    today_local = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%A %d %B %Y, %H:%M")
    return (
        "You are a friendly medical scheduling assistant on WhatsApp.\n"
        "Your job: help the patient with ONE of these:\n"
        "  (a) BOOK a new appointment\n"
        "  (b) CANCEL an existing appointment\n"
        "  (c) RESCHEDULE an existing appointment to a new time\n"
        "  (d) LIST what they have on the books\n\n"
        f"Today (Asia/Kolkata): {today_local}\n"
        f"Patient's WhatsApp wa_id (digits only): {patient_phone}\n"
        f"Patient.id in our DB: {patient_db_id}\n\n"
        "Tools:\n"
        "  - list_connected_doctors  : doctors who can take bookings\n"
        "  - find_slots              : free slots on a doctor's calendar in a window\n"
        "  - book_slot               : create a NEW appointment\n"
        "  - list_my_appointments    : the patient's upcoming appointments\n"
        "  - cancel_appointment      : cancel an existing appointment\n"
        "  - reschedule_appointment  : move an existing appointment to a new time\n\n"
        "FLOW A — BOOK NEW. Two sub-cases:\n"
        "  A1. EXACT TIME GIVEN (e.g. 'book Sat 10:30-11:00 AM', 'book 10am Monday'):\n"
        "      1. If doctor unknown → list_connected_doctors.\n"
        "      2. Call find_slots covering the requested time.\n"
        "      3. If that EXACT slot is in `free` → call book_slot IMMEDIATELY in the SAME turn.\n"
        "         No list, no 'reply YES', no confirmation question — the patient\n"
        "         already specified what they want. Just book it and confirm afterwards.\n"
        "      4. If the requested slot is NOT in `free` (taken, outside hours) → write a\n"
        "         SHORT one-line header like 'That slot is taken — pick another:' and STOP.\n"
        "         Do NOT enumerate the alternative slots in your reply text — the rendering\n"
        "         layer attaches the free slots from your most recent find_slots call as a\n"
        "         tappable WhatsApp list. Listing them in text would duplicate.\n"
        "  A2. VAGUE TIME (e.g. 'book Sunday morning', 'book next Tuesday'):\n"
        "      1. If doctor unknown → list_connected_doctors.\n"
        "      2. Call find_slots for a sensible window (9am–12pm for 'morning' etc).\n"
        "      3. Reply with a SHORT one-line header like 'Pick a slot with Dr X:' — DO NOT\n"
        "         enumerate the slots (no '1. 09:00 AM ...' list). The rendering layer ships\n"
        "         the slots from find_slots as a tappable WhatsApp list message.\n"
        "      4. On a follow-up message that names an exact ISO start/end (the picker writes\n"
        "         'Please book the slot 2026-05-04T09:00:00+05:30 to ...' when the patient\n"
        "         taps a row) → IMMEDIATELY call book_slot with those EXACT timestamps.\n"
        "  Final step (both): on book_slot ok=true → short confirmation with local time.\n\n"
        "FLOW B — CANCEL (e.g. 'cancel my appointment', 'cancel my Saturday morning'):\n"
        "  1. Call list_my_appointments.\n"
        "  2. If empty → tell the patient 'You have no upcoming appointments.' Done.\n"
        "  3. Identify which appointment they mean from their original message:\n"
        "     - exactly one returned, OR exactly one matches their criteria (e.g. day-of-week,\n"
        "       'Saturday morning') → call cancel_appointment IMMEDIATELY in the SAME turn.\n"
        "     - multiple plausible matches → present numbered list, ask which one. On their\n"
        "       numbered pick, call cancel_appointment.\n"
        "  4. After cancel_appointment returns ok=true → short confirmation. Done.\n\n"
        "FLOW C — RESCHEDULE (e.g. 'move my Sunday appt to Monday morning',\n"
        "  or 'Please reschedule my appointment id 3.' from a Reschedule button tap):\n"
        "  1. If the message ALREADY names an appointment id (e.g. 'reschedule my appointment\n"
        "     id 3') skip list_my_appointments — you HAVE the id. Otherwise call\n"
        "     list_my_appointments first to resolve which one they mean (same\n"
        "     disambiguation as Flow B).\n"
        "  2. With the chosen appointment's doctor_id, call find_slots for the new window.\n"
        "  3. Reply with a SHORT one-line header like 'Choose a new time for Dr X:' — DO NOT\n"
        "     enumerate the slots in text. The rendering layer attaches them as a tappable list.\n"
        "  4. On a follow-up message that names the appointment id + an exact ISO start/end\n"
        "     (the picker writes 'Please reschedule appointment id 3 to 2026-05-04T...') →\n"
        "     IMMEDIATELY call reschedule_appointment with those EXACT timestamps.\n"
        "  CRITICAL: in Flow C you MUST call reschedule_appointment, NEVER book_slot.\n"
        "  book_slot creates a brand-new appointment alongside the old one — that is a bug.\n"
        "  If you see 'reschedule' anywhere in the conversation history for this turn's\n"
        "  request, the slot pick MUST go to reschedule_appointment.\n\n"
        "ABSOLUTE RULES — violations are bugs:\n"
        " - YOU MAY NOT tell the patient an appointment was 'booked' / 'cancelled' /\n"
        "   'rescheduled' UNLESS the corresponding tool (book_slot / cancel_appointment /\n"
        "   reschedule_appointment) returned `\"ok\": true` IN THIS SAME TURN. Calling\n"
        "   list_my_appointments alone DOES NOT cancel anything. find_slots alone DOES NOT\n"
        "   book anything. If you have not received a fresh `ok:true` from the right tool,\n"
        "   the action HAS NOT HAPPENED — do not pretend it has.\n"
        " - DO NOT enumerate slots in your reply text (no '1. 09:00 ...' list). When\n"
        "   find_slots returns options, the rendering layer attaches them as a tappable\n"
        "   WhatsApp list — your job is just a SHORT header sentence ('Pick a slot with\n"
        "   Dr X:'). Listing them in text would duplicate and confuse the UI.\n"
        " - DO NOT say 'reply with the number' / 'pick a number' / 'reply YES' — the patient\n"
        "   uses the tappable list, not numeric replies. If only one slot exists and the\n"
        "   patient was unambiguous, just book it directly.\n"
        " - NEVER invent a time — book_slot / reschedule_appointment must use ISO timestamps\n"
        "   from a prior find_slots result you actually received.\n"
        " - A numbered pick from a list YOU JUST PROPOSED is the confirmation. Skip\n"
        "   'are you sure?' and call the tool immediately.\n"
        " - CRITICAL — appointment_id IS NOT the position number you show the patient.\n"
        "   When the patient picks #N from a numbered list, the id to pass to\n"
        "   cancel_appointment / reschedule_appointment is\n"
        "   `list_my_appointments_result.appointments[N-1].appointment_id` — NOT N.\n"
        "   For find_slots picks, use EXACT start/end ISO from `free[N-1]`.\n"
        " - For cancel_appointment and reschedule_appointment you MUST pass `patient_db_id`\n"
        "   from this prompt's header. The tool refuses cross-patient operations — if you\n"
        "   guessed an id wrong, the tool will return an ownership ERROR; correct it by\n"
        "   re-reading the appointment_id from list_my_appointments and retrying.\n"
        " - Times in replies are in the doctor's timezone.\n"
        " - Keep replies short (≤ 280 chars). WhatsApp.\n"
        " - Bare 'cancel' / 'stop' alone (NOT 'cancel my appointment') is handled outside the\n"
        "   LLM as a flow abort — you won't see those messages.\n"
        " - Pass `patient_db_id` and `patient_phone` exactly as given in this prompt.\n"
        " - On tool ERROR: ..., surface a friendly version to the patient and offer to retry\n"
        "   or escalate to a human."
    )


def _build_chat_history(state_messages: list[Any]) -> list[dict[str, Any]]:
    """Return state.messages in the OpenAI chat format.

    Already-dict entries pass through; anything that looks like a langchain
    BaseMessage is best-effort flattened to {role, content}.
    """
    out: list[dict[str, Any]] = []
    for m in state_messages or []:
        if isinstance(m, dict):
            out.append(m)
            continue
        # langchain BaseMessage compatibility
        role_attr = getattr(m, "type", None) or getattr(m, "role", None)
        content_attr = getattr(m, "content", None)
        if role_attr and content_attr is not None:
            role = {"human": "user", "ai": "assistant"}.get(role_attr, role_attr)
            out.append({"role": role, "content": content_attr})
    return out


def _sanitize_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop messages that would fail OpenAI's tool-call invariants.

    OpenAI requires:
      - every ``role=tool`` message immediately follows an ``assistant`` whose
        ``tool_calls`` contains the matching ``tool_call_id``,
      - every assistant ``tool_calls`` is followed by a ``tool`` message for
        each call (in the same conversation).

    Earlier turns that hit a transient error (e.g. our first 403 from
    /freeBusy) can leave orphan messages in the per-patient checkpoint that
    trip these invariants on the next turn. We strip them here defensively.
    """
    if not messages:
        return []

    # Pass 1: collect tool-call ids that have matching tool results that follow.
    tool_call_id_to_idx: dict[str, int] = {}
    for i, m in enumerate(messages):
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls") or []:
                tool_call_id_to_idx[tc.get("id")] = i
    satisfied: set[str] = set()
    for m in messages:
        if m.get("role") == "tool":
            tcid = m.get("tool_call_id")
            if tcid in tool_call_id_to_idx:
                satisfied.add(tcid)

    # Pass 2: emit a clean sequence — drop assistants whose tool_calls weren't all
    # satisfied, and drop tool messages whose call wasn't seen on a kept assistant.
    cleaned: list[dict[str, Any]] = []
    open_calls: set[str] = set()
    for m in messages:
        role = m.get("role")
        if role == "assistant":
            tool_calls = m.get("tool_calls") or []
            if tool_calls:
                ids = {tc.get("id") for tc in tool_calls}
                if ids.issubset(satisfied):
                    cleaned.append(m)
                    open_calls.update(ids)
                # else: drop this orphan assistant
            else:
                cleaned.append(m)
        elif role == "tool":
            tcid = m.get("tool_call_id")
            if tcid in open_calls:
                cleaned.append(m)
                open_calls.discard(tcid)
            # else: drop orphan tool
        else:
            cleaned.append(m)

    return cleaned


def _buttons_for_completion(
    last_action: str | None, appointment_id: int | None
) -> list[dict[str, str]]:
    """Reply buttons attached to the final assistant turn after a successful
    book / cancel / reschedule. Empty list otherwise.

    Button titles are phrased so the chat-sdk webhook adapter (which surfaces
    the button title as inbound `message.text`) yields a sentence that the
    intent detector + booking flow route correctly. Bare 'Cancel' alone would
    trip the flow-abort short-circuit; 'Cancel appointment' reaches the LLM."""
    if last_action == "book_slot" and appointment_id:
        return [
            {
                "id": f"cancel_appt:{appointment_id}",
                "label": "Cancel appointment",
                "action": "cancel_appointment",
            },
            {
                "id": f"reschedule_appt:{appointment_id}",
                "label": "Reschedule",
                "action": "reschedule_appointment",
            },
        ]
    if last_action == "reschedule_appointment" and appointment_id:
        return [
            {
                "id": f"cancel_appt:{appointment_id}",
                "label": "Cancel appointment",
                "action": "cancel_appointment",
            },
            {
                "id": f"reschedule_appt:{appointment_id}",
                "label": "Reschedule again",
                "action": "reschedule_appointment",
            },
        ]
    if last_action == "cancel_appointment":
        return [
            {
                "id": "book_new",
                "label": "Book another",
                "action": "book_appointment",
            },
        ]
    return []


# Regex used to detect "reschedule … (appointment) id N" anywhere in the
# conversation. Picked up at the start of the turn so the slot list rows can
# encode the right action (`slot_resched:N|...` vs `slot_book:...`).
_RESCHEDULE_TARGET_RE = re.compile(
    r"reschedule\b.*?(?:appointment|appt)?\s*(?:id\s+)?(\d+)", re.I
)


def _detect_reschedule_target(
    new_user_text: str, _state_messages: list[Any]
) -> int | None:
    """Return the appointment_id this turn's flow is rescheduling, else None.

    Only the CURRENT inbound text is consulted. We deliberately do NOT walk
    history — the per-patient LangGraph checkpoint preserves prior turns
    indefinitely, so a stale 'reschedule appt 3' from a flow that completed
    yesterday would falsely tag today's fresh booking as a reschedule. The
    webhook always rewrites a slot-row tap to include both 'reschedule' and
    the id ('Please reschedule appointment id N to ...'), so the current
    turn always carries enough context — no history walk needed."""
    m = _RESCHEDULE_TARGET_RE.search(new_user_text or "")
    return int(m.group(1)) if m else None


def _slot_row_id(
    *,
    action: str,
    doctor_id: int,
    start_iso: str,
    end_iso: str,
    appointment_id: int | None = None,
) -> str:
    """Encode a list-row id the gateway webhook can later decode into a
    precise text instruction for the LLM. Pipe-delimited so the colon stays
    available as the action-type separator."""
    if action == "reschedule" and appointment_id is not None:
        return f"slot_resched:{appointment_id}|{doctor_id}|{start_iso}|{end_iso}"
    return f"slot_book:{doctor_id}|{start_iso}|{end_iso}"


_NUMBERED_LINE_RE = re.compile(r"^\s*\d+\s*[.)]\s+", re.M)
_REPLY_NUMBER_HINT_RE = re.compile(
    r"\breply\s+with\s+the\s+number\b|\bpick\s+a\s+number\b|\breply\s+\d\b",
    re.I,
)


def _strip_enumerated_lines(text: str) -> str:
    """Defense against the LLM enumerating slots in the body when we're
    going to render a tappable list anyway. Drops lines that start like
    '1. ', '2) ', etc., AND any 'Reply with the number' hint line. Returns
    a sensible fallback if the strip leaves nothing."""
    if not text:
        return text
    cleaned_lines = [
        line
        for line in text.splitlines()
        if not _NUMBERED_LINE_RE.match(line) and not _REPLY_NUMBER_HINT_RE.search(line)
    ]
    cleaned = "\n".join(cleaned_lines).strip()
    if not cleaned:
        return "Pick one of the times below."
    return cleaned


def _build_slot_list_rows(
    *,
    free: list[dict[str, str]],
    doctor_id: int,
    doctor_name: str,
    duration_minutes: int,
    reschedule_target: int | None,
) -> list[dict[str, str]]:
    """Map a find_slots `free` array into WhatsApp list rows.

    Caps at the Meta 10-row limit. Title is the local-time string (already
    formatted by find_slots tool). Description carries doctor + duration."""
    out: list[dict[str, str]] = []
    for slot in free[:10]:
        local = slot.get("local") or slot.get("start", "")
        out.append(
            {
                "id": _slot_row_id(
                    action="reschedule" if reschedule_target else "book",
                    doctor_id=doctor_id,
                    start_iso=slot["start"],
                    end_iso=slot["end"],
                    appointment_id=reschedule_target,
                ),
                "title": local,
                "description": f"{duration_minutes} min with {doctor_name}",
            }
        )
    return out


def _looks_like_flow_abort(text: str) -> bool:
    """Bare 'cancel'/'stop'/'never mind' — abort the FLOW (not an appointment).

    Kept exact-match (not substring) so messages like 'cancel my appointment'
    fall through to the LLM, which routes them to the cancel_appointment tool.
    """
    lower = text.lower().strip().rstrip(".!?")
    return lower in {
        "cancel",
        "never mind",
        "nevermind",
        "stop",
        "abort",
        "forget it",
        "exit",
        "quit",
        "no",
        "no thanks",
        "no thank you",
    }


async def run_booking_agent(
    *,
    patient_phone: str,
    patient_db_id: int | None,
    state_messages: list[Any],
    new_user_text: str,
    flow_state: dict[str, Any] | None,
    preferred_language: str | None = None,
) -> dict[str, Any]:
    """Drive a single turn of the booking flow.

    Returns a state delta to merge into AgentState. The caller (the
    `_booking_agent_node` in :mod:`agent_workflow`) is responsible for
    enforcing routing semantics on top of this delta (e.g. adding audit
    reasons).

    ``preferred_language``: ISO-639-1 from patients.preferred_language.
    Threaded through so the LLM's natural-language replies (the part
    of each turn that isn't a tool call) are written in the patient's
    language. Tool-call arguments stay structured / English keyed.
    """
    flow_state = dict(flow_state or {})

    # Deterministic short-circuit: bare flow-abort word ("cancel", "stop", etc).
    # Substring matches like "cancel my appointment" go through the LLM so it
    # can route them to the cancel_appointment tool.
    if _looks_like_flow_abort(new_user_text):
        body = "No problem — dropped. Reply BOOK / CANCEL / RESCHEDULE anytime."
        return {
            "response_body": body,
            "current_flow": None,
            "flow_state": {},
            "messages": [{"role": "assistant", "content": body}],
        }

    llm = get_llm()
    if not llm.enabled:
        body = "Booking is offline right now (LLM disabled). Reply CALL to talk to a human."
        return {
            "response_body": body,
            "current_flow": None,
            "flow_state": {},
            "messages": [{"role": "assistant", "content": body}],
        }

    chat_history = _build_chat_history(state_messages)
    # Sanitize first — strips orphan tool_calls / orphan tool messages that
    # would trip OpenAI's "tool must follow tool_calls" invariant. Then trim
    # to the last 30 entries so the prompt stays small even on long flows.
    chat_history = _sanitize_history(chat_history)[-30:]
    messages: list[dict[str, Any]] = []
    # Language directive goes FIRST so it outranks the booking-flow
    # rules. The booking system prompt itself is English (tool names,
    # rule list); the model still picks up the directive for the
    # natural-language portions of its replies.
    if preferred_language and preferred_language != "en":
        from app import i18n

        language_hint = i18n.llm_hint(preferred_language)
        messages.append(
            {
                "role": "system",
                "content": (
                    f"You MUST write all natural-language replies to the "
                    f"patient in {language_hint}, regardless of the "
                    "language they typed in. Use the native script — do "
                    "NOT transliterate to Roman. Tool-call arguments "
                    "(JSON keys, ISO timestamps, doctor ids) stay in "
                    "English by protocol."
                ),
            }
        )
    messages.extend(
        [
            {
                "role": "system",
                "content": _system_prompt(
                    patient_db_id=patient_db_id, patient_phone=patient_phone
                ),
            },
            *chat_history,
        ]
    )
    # Track new messages we generate this turn so the reducer appends them.
    new_messages: list[dict[str, Any]] = []
    booking_completed = False
    # ``last_completed_action`` records which terminal tool succeeded this
    # turn (book / cancel / reschedule). The post-loop "final reply" branch
    # uses it to attach contextually appropriate WhatsApp reply buttons.
    last_completed_action: str | None = None
    last_completed_appt_id: int | None = None
    # Most recent successful find_slots result this turn — used to attach
    # an interactive list message with tappable rows when the LLM ends with
    # a "pick a slot" reply.
    last_find_slots: dict[str, Any] | None = None
    # Reschedule context: if user said "reschedule my appt id N" anywhere in
    # this conversation, the slot rows we emit must encode action=reschedule
    # so the row tap re-routes to reschedule_appointment (not book_slot).
    reschedule_target = _detect_reschedule_target(new_user_text, chat_history)

    SessionLocal = get_sessionmaker()

    for step in range(MAX_TOOL_STEPS):
        response = await llm.chat_with_tools(messages=messages, tools=_TOOLS)
        if response is None:
            body = (
                "I'm having trouble connecting to the scheduling assistant. "
                "Please try again in a moment, or reply CALL for help."
            )
            new_messages.append({"role": "assistant", "content": body})
            return {
                "response_body": body,
                "current_flow": "booking",  # stay in flow so retry routes back here
                "flow_state": flow_state,
                "messages": new_messages,
            }

        # Append the assistant turn (with or without tool_calls) to both the
        # in-flight message list (for the next loop iteration) and our outgoing
        # delta (so it persists across turns).
        messages.append(response)
        new_messages.append(response)

        tool_calls = response.get("tool_calls") or []
        if not tool_calls:
            # Final answer — exit the loop.
            body = (response.get("content") or "").strip() or "(no reply)"

            # If we have free slots from a find_slots this turn AND the LLM
            # didn't book/reschedule directly (booking_completed=False), ship
            # the slots as a tappable WhatsApp list. Strip any enumerated
            # lines the LLM might have emitted in violation of the prompt
            # rule — duplication confuses the user.
            list_rows: list[dict[str, str]] = []
            list_button_label: str | None = None
            list_section_title: str | None = None
            if last_find_slots and not booking_completed:
                body = _strip_enumerated_lines(body)
                list_rows = _build_slot_list_rows(
                    free=last_find_slots["free"],
                    doctor_id=last_find_slots["doctor_id"],
                    doctor_name=last_find_slots["doctor_name"],
                    duration_minutes=last_find_slots["duration_minutes"],
                    reschedule_target=reschedule_target,
                )
                list_button_label = (
                    "Pick a new time" if reschedule_target else "Pick a slot"
                )
                list_section_title = "Available times"

            # Preserve flow_state either way; on completion the supervisor still
            # exits the booking branch (current_flow=None) but downstream
            # observers can read flow_state.last_appointment_id from the audit.
            return {
                "response_body": body,
                "current_flow": None if booking_completed else "booking",
                "flow_state": flow_state,
                "messages": new_messages,
                "buttons": _buttons_for_completion(
                    last_completed_action, last_completed_appt_id
                ),
                "list_rows": list_rows,
                "list_button_label": list_button_label,
                "list_section_title": list_section_title,
            }

        # Execute every tool call requested in this assistant turn.
        async with SessionLocal() as db:
            for call in tool_calls:
                fn_name = call["function"]["name"]
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}

                impl = _TOOL_DISPATCH.get(fn_name)
                if impl is None:
                    tool_result = f"UNKNOWN_TOOL: {fn_name}"
                else:
                    try:
                        tool_result = await impl(db, args)
                    except Exception as exc:  # noqa: BLE001
                        log.exception("tool %s failed", fn_name)
                        tool_result = f"ERROR: {type(exc).__name__}: {exc}"

                # Any of the three "completing" tools succeeding clears the flow.
                if (
                    fn_name in {"book_slot", "cancel_appointment", "reschedule_appointment"}
                    and tool_result.startswith("{")
                ):
                    try:
                        parsed = json.loads(tool_result)
                        if parsed.get("ok"):
                            booking_completed = True
                            last_completed_action = fn_name
                            last_completed_appt_id = parsed.get("appointment_id")
                            flow_state = {
                                "last_action": fn_name,
                                "appointment_id": last_completed_appt_id,
                            }
                    except json.JSONDecodeError:
                        pass

                # Cache successful find_slots results so the loop's final
                # reply can attach an interactive list message of tappable
                # slot rows. We need the doctor name + timezone, so look it
                # up alongside the parsed slot list.
                if fn_name == "find_slots" and tool_result.startswith("{"):
                    try:
                        parsed = json.loads(tool_result)
                        free = parsed.get("free") or []
                        if free:
                            doc_id = int(parsed.get("doctor_id", 0))
                            doctor = await doctors_repo.get(db, doc_id) if doc_id else None
                            last_find_slots = {
                                "doctor_id": doc_id,
                                "doctor_name": doctor.name if doctor else f"Doctor #{doc_id}",
                                "duration_minutes": int(parsed.get("duration_minutes", 30)),
                                "free": free,
                            }
                    except (json.JSONDecodeError, ValueError, TypeError):
                        pass

                tool_msg = {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": tool_result,
                }
                messages.append(tool_msg)
                new_messages.append(tool_msg)

        # Loop: next iteration runs the LLM again with the tool results in scope.

    # Hit the safety cap — bail with a friendly message.
    body = (
        "I'm having trouble nailing down the booking. "
        "Reply CALL to talk to someone, or BOOK to start over."
    )
    new_messages.append({"role": "assistant", "content": body})
    return {
        "response_body": body,
        "current_flow": None,  # exit the flow so we don't loop forever
        "flow_state": {},
        "messages": new_messages,
    }

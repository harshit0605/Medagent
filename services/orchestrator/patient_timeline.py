"""Per-patient unified timeline.

Doctors don't want to bounce between five tabs to figure out
"how is this patient actually doing?". The timeline aggregates
every signal we already record into one chronologically-sorted
stream:

    - inbound + outbound WhatsApp messages (message_log)
    - dose adherence events (taken / missed / skipped / delayed)
    - side-effect ops tickets
    - appointments (booked / completed / cancelled / no-show)
    - lab follow-ups (booked / completed / reviewed)

The endpoint is read-only — no new tables, no background work.
We're paying a few extra queries on doctor-detail page load to
buy a much better one-glance summary.

Why server-side merge + format instead of client-side?

    1. Five tables with different keying conventions
       (``patient_id`` is the FK on adherence_events / labs /
       appointments but the phone string on message_log /
       ops_tickets) — keeping that hidden behind one endpoint
       means the UI doesn't have to learn the inconsistency.
    2. Title formatting (``"Took metformin 500mg"`` vs
       ``"Missed nightly insulin"``) lives next to the data; we
       avoid duplicating it in every UI consumer.
    3. The ``since/until`` filter caps total payload — clients
       don't need to over-fetch and discard.

Schema is a discriminated union on ``kind``. Adding new kinds
(e.g. ``recap_acknowledged``) only requires extending the
``_KIND_*`` constants and a new gather step — UI and tests are
unaffected for kinds they don't care about.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    AdherenceEvent,
    AdherenceStatus,
    Appointment,
    AppointmentStatus,
    ClinicalAlert,
    LabFollowup,
    MessageDirection,
    MessageLog,
    OpsTicket,
    Patient,
)

log = logging.getLogger(__name__)


TimelineKind = Literal[
    "message_inbound",
    "message_outbound",
    "dose_taken",
    "dose_missed",
    "dose_skipped",
    "dose_delayed",
    "side_effect",
    "appointment_booked",
    "appointment_completed",
    "appointment_cancelled",
    "appointment_no_show",
    "lab_booked",
    "lab_completed",
    "lab_reviewed",
    "clinical_alert_high",
    "clinical_alert_critical",
]


# Default kinds returned when caller doesn't specify a filter.
# We INCLUDE outbound bot messages by default — doctors want to
# see what the bot has been saying, not just what the patient
# said.
ALL_KINDS: tuple[TimelineKind, ...] = (
    "message_inbound",
    "message_outbound",
    "dose_taken",
    "dose_missed",
    "dose_skipped",
    "dose_delayed",
    "side_effect",
    "appointment_booked",
    "appointment_completed",
    "appointment_cancelled",
    "appointment_no_show",
    "lab_booked",
    "lab_completed",
    "lab_reviewed",
    "clinical_alert_high",
    "clinical_alert_critical",
)


_DOSE_KIND_BY_STATUS: dict[AdherenceStatus, TimelineKind] = {
    AdherenceStatus.taken: "dose_taken",
    AdherenceStatus.missed: "dose_missed",
    AdherenceStatus.skipped: "dose_skipped",
    AdherenceStatus.delayed: "dose_delayed",
}


_APPT_KIND_BY_STATUS: dict[AppointmentStatus, TimelineKind] = {
    AppointmentStatus.confirmed: "appointment_booked",
    AppointmentStatus.completed: "appointment_completed",
    AppointmentStatus.cancelled: "appointment_cancelled",
    AppointmentStatus.no_show: "appointment_no_show",
}


@dataclass
class TimelineEvent:
    """One row in the unified patient timeline.

    ``occurred_at`` is the moment the event happened from the
    patient's perspective (dose's scheduled time, appointment's
    start, message receipt). ``title`` is a one-line summary
    formatted server-side; ``body`` carries optional secondary
    text (e.g. message body excerpt). ``detail`` keeps
    type-specific extras (entity ids for click-through, badges)
    so the UI can render rich payloads without us having to
    expand the discriminated union for every UI tweak.
    """

    occurred_at: datetime
    kind: TimelineKind
    title: str
    body: str | None
    detail: dict[str, Any]


def _ensure_utc(value: datetime) -> datetime:
    """Normalise naive datetimes — Postgres TIMESTAMPTZ columns
    sometimes come back without tzinfo through psycopg/SQLA when
    the connection's timezone setting drifts. Treating naive as
    UTC matches what the dispatcher and other readers do."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _truncate(text: str, *, max_len: int = 140) -> str:
    """Trim long bodies for the timeline preview. Doctors click
    through to the raw message log when they want the full
    text — the timeline is the skim view."""
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _extract_message_body(raw: dict[str, Any]) -> str:
    """Pull a human-readable body out of the polymorphic
    ``message_log.message`` JSON. Inbound messages use
    ``{"type": "text", "text": {"body": "..."}}`` (Meta's
    payload) — outbound rows mirror what we sent which can be
    text or template. Falls back to a kind-tag if we can't
    extract anything useful, so the timeline still shows the
    event happened."""
    if not isinstance(raw, dict):
        return ""
    # Meta inbound text shape.
    text_obj = raw.get("text")
    if isinstance(text_obj, dict):
        body = text_obj.get("body")
        if isinstance(body, str) and body.strip():
            return body
    # Outbound flat shape we sometimes use.
    body = raw.get("body")
    if isinstance(body, str) and body.strip():
        return body
    # Templates carry a name but no inline body — show the name.
    template = raw.get("template")
    if isinstance(template, dict):
        name = template.get("name")
        if isinstance(name, str):
            return f"[template: {name}]"
    kind = raw.get("type") or raw.get("payload_kind")
    if isinstance(kind, str):
        return f"[{kind}]"
    return ""


# ---- per-source gatherers ---------------------------------------------------


async def _gather_messages(
    session: AsyncSession, *, phone: str, since: datetime, until: datetime
) -> list[TimelineEvent]:
    if not phone:
        return []
    stmt = (
        select(MessageLog)
        .where(MessageLog.patient_id == phone)
        .where(MessageLog.occurred_at >= since)
        .where(MessageLog.occurred_at <= until)
        .order_by(MessageLog.occurred_at.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    out: list[TimelineEvent] = []
    for row in rows:
        body = _extract_message_body(row.message or {})
        is_inbound = row.direction == MessageDirection.inbound
        kind: TimelineKind = (
            "message_inbound" if is_inbound else "message_outbound"
        )
        title = (
            "Patient message" if is_inbound else "Bot reply"
        )
        out.append(
            TimelineEvent(
                occurred_at=_ensure_utc(row.occurred_at),
                kind=kind,
                title=title,
                body=_truncate(body) if body else None,
                detail={
                    "message_id": row.id,
                    "wamid": row.wamid,
                    "payload_kind": (
                        row.payload_kind.value
                        if row.payload_kind is not None
                        else None
                    ),
                },
            )
        )
    return out


async def _gather_doses(
    session: AsyncSession,
    *,
    patient_db_id: int,
    since: datetime,
    until: datetime,
) -> list[TimelineEvent]:
    # We surface only "interesting" adherence states — pending
    # rows are scheduled but not yet acted on, so they're
    # forecast not history. The patient detail page already
    # has an "upcoming" section for those.
    stmt = (
        select(AdherenceEvent)
        .where(AdherenceEvent.patient_id == patient_db_id)
        .where(AdherenceEvent.scheduled_at >= since)
        .where(AdherenceEvent.scheduled_at <= until)
        .where(
            AdherenceEvent.status.in_(list(_DOSE_KIND_BY_STATUS))
        )
        .options(selectinload(AdherenceEvent.regimen))
        .order_by(AdherenceEvent.scheduled_at.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    out: list[TimelineEvent] = []
    for row in rows:
        kind = _DOSE_KIND_BY_STATUS[row.status]
        regimen = row.regimen
        med_name = (
            getattr(regimen, "medication_name", None)
            if regimen is not None
            else None
        )
        # Title reads like "Took metformin" / "Missed insulin".
        verb = {
            AdherenceStatus.taken: "Took",
            AdherenceStatus.missed: "Missed",
            AdherenceStatus.skipped: "Skipped",
            AdherenceStatus.delayed: "Delayed",
        }[row.status]
        title = (
            f"{verb} {med_name}" if med_name else f"{verb} dose"
        )
        # Use confirmed_at as the occurrence timestamp when the
        # patient confirmed (taken/skipped/delayed) — that's
        # when the action *actually* happened. For missed (no
        # confirmation), fall back to scheduled_at.
        occurred_at = (
            _ensure_utc(row.confirmed_at)
            if row.confirmed_at is not None
            else _ensure_utc(row.scheduled_at)
        )
        out.append(
            TimelineEvent(
                occurred_at=occurred_at,
                kind=kind,
                title=title,
                body=None,
                detail={
                    "adherence_event_id": row.id,
                    "regimen_id": row.regimen_id,
                    "scheduled_at": _ensure_utc(
                        row.scheduled_at
                    ).isoformat(),
                },
            )
        )
    return out


async def _gather_side_effects(
    session: AsyncSession, *, phone: str, since: datetime, until: datetime
) -> list[TimelineEvent]:
    if not phone:
        return []
    stmt = (
        select(OpsTicket)
        .where(OpsTicket.patient_id == phone)
        .where(OpsTicket.category == "side_effect_report")
        .where(OpsTicket.created_at >= since)
        .where(OpsTicket.created_at <= until)
        .order_by(OpsTicket.created_at.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    out: list[TimelineEvent] = []
    for row in rows:
        # The side-effect handler stores the patient's quote in
        # ticket notes prefixed with "patient said:". Pull that
        # out as the body for skim purposes.
        body: str | None = None
        if row.notes:
            for line in row.notes.splitlines():
                lower = line.strip().lower()
                if lower.startswith("patient said:"):
                    body = line.split(":", 1)[1].strip()
                    break
        out.append(
            TimelineEvent(
                occurred_at=_ensure_utc(row.created_at),
                kind="side_effect",
                title="Side effect reported",
                body=_truncate(body) if body else None,
                detail={
                    "ticket_id": row.id,
                    "status": row.status.value,
                    "priority": row.priority,
                },
            )
        )
    return out


async def _gather_appointments(
    session: AsyncSession,
    *,
    patient_db_id: int,
    since: datetime,
    until: datetime,
) -> list[TimelineEvent]:
    # Filter on scheduled_for since that's what the doctor
    # cares about — "when is/was the appointment", not "when
    # was the row written". We INCLUDE proposed status as
    # implicit-skip so the timeline doesn't get noisy with
    # every slot the bot floated.
    stmt = (
        select(Appointment)
        .where(Appointment.patient_id == patient_db_id)
        .where(Appointment.scheduled_for >= since)
        .where(Appointment.scheduled_for <= until)
        .where(
            Appointment.status.in_(list(_APPT_KIND_BY_STATUS))
        )
        .order_by(Appointment.scheduled_for.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    out: list[TimelineEvent] = []
    for row in rows:
        kind = _APPT_KIND_BY_STATUS[row.status]
        verb = {
            AppointmentStatus.confirmed: "Appointment booked",
            AppointmentStatus.completed: "Appointment completed",
            AppointmentStatus.cancelled: "Appointment cancelled",
            AppointmentStatus.no_show: "Appointment no-show",
        }[row.status]
        out.append(
            TimelineEvent(
                occurred_at=_ensure_utc(row.scheduled_for),
                kind=kind,
                title=verb,
                body=row.summary,
                detail={
                    "appointment_id": row.id,
                    "doctor_id": row.doctor_id,
                    "calendar_event_id": row.calendar_event_id,
                    "calendar_html_link": row.calendar_html_link,
                },
            )
        )
    return out


async def _gather_lab_followups(
    session: AsyncSession,
    *,
    patient_db_id: int,
    since: datetime,
    until: datetime,
) -> list[TimelineEvent]:
    """Lab events use the timestamp on whichever transition fired
    in [since, until] — booked_at, completed_at, or
    reviewed_at. A single lab can produce up to three timeline
    entries (one per transition) which is correct: the doctor
    wants to see "scheduled CBC", "completed CBC", "reviewed
    CBC" as separate beats."""
    # Pull rows where ANY of the three transition timestamps
    # falls in window. Postgres OR over indexed columns is fine
    # at our scale.
    stmt = (
        select(LabFollowup)
        .where(LabFollowup.patient_id == patient_db_id)
        .where(
            (
                (LabFollowup.booked_at >= since)
                & (LabFollowup.booked_at <= until)
            )
            | (
                (LabFollowup.completed_at >= since)
                & (LabFollowup.completed_at <= until)
            )
            | (
                (LabFollowup.reviewed_at >= since)
                & (LabFollowup.reviewed_at <= until)
            )
        )
    )
    rows = (await session.execute(stmt)).scalars().all()
    out: list[TimelineEvent] = []
    for row in rows:
        if row.booked_at is not None and since <= _ensure_utc(
            row.booked_at
        ) <= until:
            out.append(
                TimelineEvent(
                    occurred_at=_ensure_utc(row.booked_at),
                    kind="lab_booked",
                    title=f"Lab booked: {row.test_name}",
                    body=None,
                    detail={"lab_followup_id": row.id},
                )
            )
        if row.completed_at is not None and since <= _ensure_utc(
            row.completed_at
        ) <= until:
            out.append(
                TimelineEvent(
                    occurred_at=_ensure_utc(row.completed_at),
                    kind="lab_completed",
                    title=f"Lab completed: {row.test_name}",
                    body=None,
                    detail={"lab_followup_id": row.id},
                )
            )
        if row.reviewed_at is not None and since <= _ensure_utc(
            row.reviewed_at
        ) <= until:
            out.append(
                TimelineEvent(
                    occurred_at=_ensure_utc(row.reviewed_at),
                    kind="lab_reviewed",
                    title=f"Lab reviewed: {row.test_name}",
                    body=None,
                    detail={"lab_followup_id": row.id},
                )
            )
    return out


async def _gather_clinical_alerts(
    session: AsyncSession,
    *,
    patient_db_id: int,
    since: datetime,
    until: datetime,
) -> list[TimelineEvent]:
    """Clinical-alert events from the slice-10 triage classifier.
    Surfaces high + critical severity rows as distinct kinds so
    the timeline UI can pick a different colour without parsing
    the detail dict. We deliberately use the alert's
    ``created_at`` (not ``acknowledged_at`` or ``resolved_at``)
    as the timeline timestamp — what the doctor cares about is
    "when did the safety signal fire?", not "when was it
    handled"."""
    stmt = (
        select(ClinicalAlert)
        .where(ClinicalAlert.patient_id == patient_db_id)
        .where(ClinicalAlert.created_at >= since)
        .where(ClinicalAlert.created_at <= until)
        .order_by(ClinicalAlert.created_at.desc())
    )
    rows = (await session.execute(stmt)).scalars().all()
    out: list[TimelineEvent] = []
    for row in rows:
        if row.severity == "critical":
            kind: TimelineKind = "clinical_alert_critical"
            title = "🚨 Critical clinical alert"
        elif row.severity == "high":
            kind = "clinical_alert_high"
            title = "⚠️ High clinical alert"
        else:
            # Defensive — alerts shouldn't be persisted with
            # other severities, but if they are we don't want
            # to mistype them as one of our kinds.
            continue
        out.append(
            TimelineEvent(
                occurred_at=_ensure_utc(row.created_at),
                kind=kind,
                title=title,
                body=row.clinical_summary,
                detail={
                    "alert_id": row.id,
                    "status": row.status,
                    "red_flags": list(row.red_flags or []),
                    "paged_attempts": row.paged_attempts or 0,
                    "paged_doctor_id": row.paged_doctor_id,
                },
            )
        )
    return out


# ---- top-level orchestration ------------------------------------------------


async def build_timeline(
    session: AsyncSession,
    *,
    patient_db_id: int,
    since: datetime,
    until: datetime,
    kinds: tuple[TimelineKind, ...] | None = None,
    limit: int = 200,
) -> list[TimelineEvent]:
    """Aggregate every signal source into one chronological
    stream, newest first.

    ``kinds`` filters the output. If omitted we return every
    kind. Filtering happens AFTER gather so a single source
    that emits multiple kinds (e.g. lab_followups → booked +
    completed + reviewed) plays well with mixed filters.

    ``limit`` is a hard cap on the returned list; sources that
    would overflow it are still queried in full but truncated
    after merge — the cost saving from truncating mid-gather
    isn't worth the extra logic at our patient-history scale.
    """
    patient = await session.get(Patient, patient_db_id)
    if patient is None:
        raise ValueError(f"patient {patient_db_id} not found")

    phone = patient.phone or ""

    # Gather. Could be parallelised with asyncio.gather but the
    # AsyncSession isn't safe for concurrent statements — would
    # need separate sessions per source. Sequential is fine for
    # now (5–6 quick indexed lookups).
    events: list[TimelineEvent] = []
    events.extend(
        await _gather_messages(
            session, phone=phone, since=since, until=until
        )
    )
    events.extend(
        await _gather_doses(
            session,
            patient_db_id=patient_db_id,
            since=since,
            until=until,
        )
    )
    events.extend(
        await _gather_side_effects(
            session, phone=phone, since=since, until=until
        )
    )
    events.extend(
        await _gather_appointments(
            session,
            patient_db_id=patient_db_id,
            since=since,
            until=until,
        )
    )
    events.extend(
        await _gather_lab_followups(
            session,
            patient_db_id=patient_db_id,
            since=since,
            until=until,
        )
    )
    events.extend(
        await _gather_clinical_alerts(
            session,
            patient_db_id=patient_db_id,
            since=since,
            until=until,
        )
    )

    if kinds is not None:
        kinds_set = set(kinds)
        events = [e for e in events if e.kind in kinds_set]

    events.sort(key=lambda e: e.occurred_at, reverse=True)
    if limit and len(events) > limit:
        events = events[:limit]
    return events

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AdherenceEvent,
    AdherenceStatus,
    AppointmentFollowup,
    AppointmentRecap,
    FollowupStatus,
    InboundClassification,
    LabFollowup,
    MissRecoveryAction,
    MissRecoveryEvent,
    OpsTicket,
    OpsTicketStatus,
    Prescription,
    RecapStatus,
    Regimen,
    VerificationStatus,
)


def _ratio(numerator: int, denominator: int) -> float:
    if not denominator:
        return 0.0
    return round(numerator / denominator, 4)


async def adherence_rate(session: AsyncSession) -> float:
    total = (await session.execute(select(func.count(AdherenceEvent.id)))).scalar_one()
    taken = (
        await session.execute(
            select(func.count(AdherenceEvent.id)).where(
                AdherenceEvent.status == AdherenceStatus.taken
            )
        )
    ).scalar_one()
    return _ratio(taken, total)


async def refill_risk_rate(session: AsyncSession) -> float:
    total = (
        await session.execute(
            select(func.count(MissRecoveryEvent.id)).where(
                MissRecoveryEvent.action.in_(
                    [MissRecoveryAction.refill_support, MissRecoveryAction.reschedule]
                )
            )
        )
    ).scalar_one()
    risky = (
        await session.execute(
            select(func.count(MissRecoveryEvent.id)).where(
                MissRecoveryEvent.action == MissRecoveryAction.refill_support
            )
        )
    ).scalar_one()
    return _ratio(risky, total)


async def followup_closure_rate(session: AsyncSession) -> float:
    total_labs = (await session.execute(select(func.count(LabFollowup.id)))).scalar_one()
    total_appts = (
        await session.execute(select(func.count(AppointmentFollowup.id)))
    ).scalar_one()
    closed_labs = (
        await session.execute(
            select(func.count(LabFollowup.id)).where(
                LabFollowup.status == FollowupStatus.reviewed
            )
        )
    ).scalar_one()
    closed_appts = (
        await session.execute(
            select(func.count(AppointmentFollowup.id)).where(
                AppointmentFollowup.status == FollowupStatus.reviewed
            )
        )
    ).scalar_one()
    return _ratio(closed_labs + closed_appts, total_labs + total_appts)


async def program_metrics(session: AsyncSession) -> dict[str, float]:
    return {
        "adherence_rate": await adherence_rate(session),
        "refill_risk_rate": await refill_risk_rate(session),
        "followup_closure_rate": await followup_closure_rate(session),
    }


async def regimens_running_low_count(
    session: AsyncSession, *, threshold_days: int = 7, today: date | None = None
) -> int:
    """Active regimens with supply tracking whose supply runs out within
    ``threshold_days``. Active = ``ends_on`` null or in the future.

    Note: Postgres doesn't support ``Date + Integer`` arithmetic without
    an explicit interval cast; we filter in Python after a narrow SQL
    pre-filter — fine for this scale."""
    today = today or date.today()
    cutoff = today + timedelta(days=threshold_days)
    rows = list(
        (
            await session.execute(
                select(Regimen).where(
                    Regimen.supply_days_initial.isnot(None),
                    Regimen.supply_started_on.isnot(None),
                    or_(Regimen.ends_on.is_(None), Regimen.ends_on >= today),
                )
            )
        )
        .scalars()
        .all()
    )
    return sum(
        1
        for r in rows
        if (r.supply_started_on + timedelta(days=r.supply_days_initial)) <= cutoff
    )


async def missed_dose_escalations_open_count(session: AsyncSession) -> int:
    """Open / acknowledged ops_tickets in the missed_doses category."""
    return (
        await session.execute(
            select(func.count(OpsTicket.id)).where(
                and_(
                    OpsTicket.category == "missed_doses",
                    OpsTicket.status.in_(
                        [
                            OpsTicketStatus.open,
                            OpsTicketStatus.acknowledged,
                        ]
                    ),
                )
            )
        )
    ).scalar_one()


async def refill_help_open_count(session: AsyncSession) -> int:
    """Open / acknowledged refill_help tickets — the patient asked for
    help OR hit the snooze cap."""
    return (
        await session.execute(
            select(func.count(OpsTicket.id)).where(
                and_(
                    OpsTicket.category == "refill_help",
                    OpsTicket.status.in_(
                        [
                            OpsTicketStatus.open,
                            OpsTicketStatus.acknowledged,
                        ]
                    ),
                )
            )
        )
    ).scalar_one()


async def labs_overdue_count(
    session: AsyncSession, *, today: date | None = None
) -> int:
    """Lab follow-ups still in due/booked state with due_by in the past."""
    today = today or date.today()
    return (
        await session.execute(
            select(func.count(LabFollowup.id)).where(
                and_(
                    LabFollowup.status.in_(
                        [FollowupStatus.due, FollowupStatus.booked]
                    ),
                    LabFollowup.due_by.isnot(None),
                    LabFollowup.due_by < today,
                )
            )
        )
    ).scalar_one()


async def prescriptions_pending_count(session: AsyncSession) -> int:
    """Prescriptions awaiting clinician review."""
    return (
        await session.execute(
            select(func.count(Prescription.id)).where(
                Prescription.human_verification_status
                == VerificationStatus.pending
            )
        )
    ).scalar_one()


async def tickets_sla_overdue_count(session: AsyncSession) -> int:
    """Open / acknowledged tickets whose SLA has elapsed (created_at + sla
    is in the past), excluding currently-snoozed ones — those are
    intentionally deferred and shouldn't show as overdue."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    rows = list(
        (
            await session.execute(
                select(OpsTicket).where(
                    OpsTicket.status.in_(
                        [OpsTicketStatus.open, OpsTicketStatus.acknowledged]
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    overdue = 0
    for t in rows:
        if (
            t.snoozed_until is not None and t.snoozed_until > now
        ):  # currently snoozed — skip
            continue
        created = t.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        deadline = created + timedelta(minutes=t.sla_minutes)
        if deadline <= now:
            overdue += 1
    return overdue


async def lab_help_open_count(session: AsyncSession) -> int:
    """Open / acknowledged lab_help tickets."""
    return (
        await session.execute(
            select(func.count(OpsTicket.id)).where(
                and_(
                    OpsTicket.category == "lab_help",
                    OpsTicket.status.in_(
                        [
                            OpsTicketStatus.open,
                            OpsTicketStatus.acknowledged,
                        ]
                    ),
                )
            )
        )
    ).scalar_one()


async def care_gaps_open_count(session: AsyncSession) -> int:
    """Patients in any cohort who are overdue for their standing
    care-plan test. Pure pass-through to the scheduler module so the
    sweep and the tile share the same definition of 'gap'."""
    # Local import so the dashboard module doesn't pull in the scheduler
    # service at startup (a) for callers that don't need it and (b) to
    # avoid the circular-ish dependency on a sibling service package.
    from services.scheduler import care_gaps as care_gaps_module

    return await care_gaps_module.overdue_care_gap_count(session)


# ---- Program-level analytics aggregator -----------------------------------
# Returns a single denormalized snapshot powering the /analytics page.
# All counts are window-scoped (default 30d) so callers can compare
# "this month vs last month" without time-travel queries on the DB.


async def adherence_rate_window(
    session: AsyncSession, *, since: datetime
) -> dict[str, int | float]:
    """Adherence numbers since ``since``. Mirrors the per-patient
    adherence_summary helper but program-wide. Adherence rate is
    ``taken / (taken + missed + skipped)`` so untouched ``scheduled``
    rows don't drag the rate down for events that haven't matured yet.
    """
    counts = {"taken": 0, "missed": 0, "skipped": 0, "delayed": 0, "scheduled": 0}
    rows = (
        await session.execute(
            select(AdherenceEvent.status, func.count(AdherenceEvent.id))
            .where(AdherenceEvent.scheduled_at >= since)
            .group_by(AdherenceEvent.status)
        )
    ).all()
    for status, count in rows:
        key = status.value if hasattr(status, "value") else str(status)
        if key in counts:
            counts[key] = count
    completed = counts["taken"] + counts["missed"] + counts["skipped"]
    rate = (counts["taken"] / completed) if completed else 0.0
    total = sum(counts.values())
    return {
        "total": total,
        "taken": counts["taken"],
        "missed": counts["missed"],
        "skipped": counts["skipped"],
        "delayed": counts["delayed"],
        "scheduled": counts["scheduled"],
        "rate": round(rate, 4),
    }


async def recap_funnel_window(
    session: AsyncSession, *, since: datetime
) -> dict[str, int | float]:
    """Recap delivery + acknowledgement funnel since ``since``. ``sent``
    is the denominator for the ack rate — drafts that never sent
    don't count. ``acknowledged`` is the success state; ``questioned``
    counts as engaged-but-needs-followup, separate from acked."""
    rows = (
        await session.execute(
            select(AppointmentRecap.status, func.count(AppointmentRecap.id))
            .where(AppointmentRecap.created_at >= since)
            .group_by(AppointmentRecap.status)
        )
    ).all()
    counts = {s.value: 0 for s in RecapStatus}
    for status, count in rows:
        key = status.value if hasattr(status, "value") else str(status)
        counts[key] = count
    sent_total = (
        counts["sent"] + counts["acknowledged"] + counts["questioned"]
    )
    ack_rate = (
        counts["acknowledged"] / sent_total if sent_total else 0.0
    )
    return {
        "draft": counts["draft"],
        "sent": counts["sent"],
        "acknowledged": counts["acknowledged"],
        "questioned": counts["questioned"],
        "sent_total": sent_total,
        "ack_rate": round(ack_rate, 4),
    }


async def inbox_composition_window(
    session: AsyncSession, *, since: datetime
) -> dict[str, dict[str, int]]:
    """{by_category: {...}, by_urgency: {...}, by_input_kind: {...}}
    for inbound_classifications since ``since``. Skips any classification
    not yet recorded (e.g. action_tap on a 0-LLM message)."""
    by_category_rows = (
        await session.execute(
            select(
                InboundClassification.category,
                func.count(InboundClassification.id),
            )
            .where(InboundClassification.created_at >= since)
            .group_by(InboundClassification.category)
        )
    ).all()
    by_urgency_rows = (
        await session.execute(
            select(
                InboundClassification.urgency,
                func.count(InboundClassification.id),
            )
            .where(InboundClassification.created_at >= since)
            .group_by(InboundClassification.urgency)
        )
    ).all()
    by_input_kind_rows = (
        await session.execute(
            select(
                InboundClassification.input_kind,
                func.count(InboundClassification.id),
            )
            .where(InboundClassification.created_at >= since)
            .group_by(InboundClassification.input_kind)
        )
    ).all()
    return {
        "by_category": {cat: cnt for cat, cnt in by_category_rows},
        "by_urgency": {urg: cnt for urg, cnt in by_urgency_rows},
        "by_input_kind": {ik: cnt for ik, cnt in by_input_kind_rows},
    }


async def ops_queue_window(
    session: AsyncSession, *, since: datetime
) -> dict[str, int | float | None]:
    """Open count + open-by-priority + opened/resolved counts in window
    + median resolve time (in minutes). Median is computed in Python to
    keep the SQL portable across Postgres versions."""
    open_total = (
        await session.execute(
            select(func.count(OpsTicket.id)).where(
                OpsTicket.status.in_(
                    [OpsTicketStatus.open, OpsTicketStatus.acknowledged]
                )
            )
        )
    ).scalar_one()
    by_prio_rows = (
        await session.execute(
            select(OpsTicket.priority, func.count(OpsTicket.id))
            .where(
                OpsTicket.status.in_(
                    [OpsTicketStatus.open, OpsTicketStatus.acknowledged]
                )
            )
            .group_by(OpsTicket.priority)
        )
    ).all()
    by_priority = {p: c for p, c in by_prio_rows}

    opened_in_window = (
        await session.execute(
            select(func.count(OpsTicket.id)).where(
                OpsTicket.created_at >= since
            )
        )
    ).scalar_one()
    resolved_rows = (
        await session.execute(
            select(OpsTicket.created_at, OpsTicket.resolved_at).where(
                OpsTicket.resolved_at.isnot(None),
                OpsTicket.resolved_at >= since,
            )
        )
    ).all()
    resolved_in_window = len(resolved_rows)

    resolve_times_min: list[float] = []
    for created_at, resolved_at in resolved_rows:
        c = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
        r = resolved_at if resolved_at.tzinfo else resolved_at.replace(tzinfo=timezone.utc)
        resolve_times_min.append((r - c).total_seconds() / 60.0)
    median: float | None
    if resolve_times_min:
        sorted_times = sorted(resolve_times_min)
        n = len(sorted_times)
        if n % 2 == 1:
            median = sorted_times[n // 2]
        else:
            median = (sorted_times[n // 2 - 1] + sorted_times[n // 2]) / 2.0
        median = round(median, 1)
    else:
        median = None

    return {
        "open_total": open_total,
        "by_priority": by_priority,
        "opened_in_window": opened_in_window,
        "resolved_in_window": resolved_in_window,
        "median_resolve_minutes": median,
    }


async def analytics_snapshot(
    session: AsyncSession, *, days: int = 30
) -> dict[str, dict | int | float]:
    """One-shot aggregator for the /analytics page. Pure read; no
    writes; cheap enough to render synchronously on every page load."""
    if days < 1:
        days = 1
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return {
        "window_days": days,
        "since": cutoff,
        "adherence": await adherence_rate_window(session, since=cutoff),
        "recap_funnel": await recap_funnel_window(session, since=cutoff),
        "inbox": await inbox_composition_window(session, since=cutoff),
        "ops_queue": await ops_queue_window(session, since=cutoff),
    }


# ---- Daily time-series buckets --------------------------------------------
# Each helper returns a list of ``{date, ...counts}`` dicts in ascending
# date order. Days with zero events still appear (zero-filled) so
# sparklines render with consistent x-spacing. Cutoff is inclusive of
# today.


def _zero_filled_dates(days: int) -> list[date]:
    today = datetime.now(timezone.utc).date()
    return [today - timedelta(days=days - 1 - i) for i in range(days)]


async def daily_adherence_buckets(
    session: AsyncSession, *, days: int = 30
) -> list[dict[str, int | float | str]]:
    """Per-day adherence: taken / missed / skipped / scheduled counts +
    daily ``rate`` (taken / matured). Bucketing is on
    ``scheduled_at::date`` so a dose that fired at 8 AM and was confirmed
    at 9 AM stays on its own day. Dose-level events that landed in the
    future are bucketed too (status=scheduled), which lets the
    sparkline show "what's coming" alongside what's already played out."""
    if days < 1:
        days = 1
    today = datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=days - 1)

    # GROUP BY (date, status). Postgres date_trunc + cast handles the
    # timezone correctly because scheduled_at is timestamptz.
    bucket_col = func.date(AdherenceEvent.scheduled_at).label("bucket_d")
    rows = (
        await session.execute(
            select(
                bucket_col,
                AdherenceEvent.status,
                func.count(AdherenceEvent.id),
            )
            .where(func.date(AdherenceEvent.scheduled_at) >= cutoff)
            .group_by(bucket_col, AdherenceEvent.status)
        )
    ).all()

    by_day: dict[date, dict[str, int]] = {
        d: {
            "taken": 0,
            "missed": 0,
            "skipped": 0,
            "delayed": 0,
            "scheduled": 0,
        }
        for d in _zero_filled_dates(days)
    }
    for bucket, status, count in rows:
        # bucket may come back as a datetime (Postgres date_trunc casts)
        # or a date — normalize.
        if isinstance(bucket, datetime):
            bucket_d = bucket.date()
        else:
            bucket_d = bucket
        if bucket_d not in by_day:
            continue
        key = status.value if hasattr(status, "value") else str(status)
        if key in by_day[bucket_d]:
            by_day[bucket_d][key] = count

    out: list[dict[str, int | float | str]] = []
    for d, c in sorted(by_day.items()):
        matured = c["taken"] + c["missed"] + c["skipped"]
        rate = (c["taken"] / matured) if matured else 0.0
        out.append(
            {
                "date": d.isoformat(),
                "taken": c["taken"],
                "missed": c["missed"],
                "skipped": c["skipped"],
                "delayed": c["delayed"],
                "scheduled": c["scheduled"],
                "rate": round(rate, 4),
            }
        )
    return out


async def daily_inbox_buckets(
    session: AsyncSession, *, days: int = 30
) -> list[dict[str, int | str]]:
    """Per-day inbound count + breakdown by urgency. Critical/High
    daily counts are the doctor's "where do I focus" view."""
    if days < 1:
        days = 1
    today = datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=days - 1)

    bucket_col = func.date(InboundClassification.created_at).label(
        "bucket_i"
    )
    rows = (
        await session.execute(
            select(
                bucket_col,
                InboundClassification.urgency,
                func.count(InboundClassification.id),
            )
            .where(func.date(InboundClassification.created_at) >= cutoff)
            .group_by(bucket_col, InboundClassification.urgency)
        )
    ).all()

    by_day: dict[date, dict[str, int]] = {
        d: {"critical": 0, "high": 0, "medium": 0, "low": 0, "total": 0}
        for d in _zero_filled_dates(days)
    }
    for bucket, urgency, count in rows:
        if isinstance(bucket, datetime):
            bucket_d = bucket.date()
        else:
            bucket_d = bucket
        if bucket_d not in by_day:
            continue
        if urgency in by_day[bucket_d]:
            by_day[bucket_d][urgency] = count
            by_day[bucket_d]["total"] += count

    return [
        {"date": d.isoformat(), **c} for d, c in sorted(by_day.items())
    ]


async def daily_recap_buckets(
    session: AsyncSession, *, days: int = 30
) -> list[dict[str, int | str]]:
    """Per-day recap sent count + acknowledged count. Bucketed on
    ``sent_at::date`` for the sent path and ``acknowledged_at::date``
    for acks — a recap sent on day N and acked on day N+1 contributes
    to two different buckets, which is what we want for funnel-by-day."""
    if days < 1:
        days = 1
    today = datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=days - 1)

    sent_col = func.date(AppointmentRecap.sent_at).label("bucket_s")
    sent_rows = (
        await session.execute(
            select(sent_col, func.count(AppointmentRecap.id))
            .where(AppointmentRecap.sent_at.isnot(None))
            .where(func.date(AppointmentRecap.sent_at) >= cutoff)
            .group_by(sent_col)
        )
    ).all()
    ack_col = func.date(AppointmentRecap.acknowledged_at).label("bucket_a")
    ack_rows = (
        await session.execute(
            select(ack_col, func.count(AppointmentRecap.id))
            .where(AppointmentRecap.acknowledged_at.isnot(None))
            .where(
                func.date(AppointmentRecap.acknowledged_at) >= cutoff
            )
            .group_by(ack_col)
        )
    ).all()

    by_day: dict[date, dict[str, int]] = {
        d: {"sent": 0, "acked": 0} for d in _zero_filled_dates(days)
    }
    for bucket, count in sent_rows:
        d = bucket.date() if isinstance(bucket, datetime) else bucket
        if d in by_day:
            by_day[d]["sent"] = count
    for bucket, count in ack_rows:
        d = bucket.date() if isinstance(bucket, datetime) else bucket
        if d in by_day:
            by_day[d]["acked"] = count

    return [
        {"date": d.isoformat(), **c} for d, c in sorted(by_day.items())
    ]


async def daily_ticket_buckets(
    session: AsyncSession, *, days: int = 30
) -> list[dict[str, int | str]]:
    """Per-day tickets opened + tickets resolved. ``opened`` buckets on
    ``created_at``; ``resolved`` on ``resolved_at``. Same dual-bucket
    semantics as recaps — open on day N, resolve on day N+2, two
    different buckets contribute."""
    if days < 1:
        days = 1
    today = datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=days - 1)

    opened_col = func.date(OpsTicket.created_at).label("bucket_o")
    opened_rows = (
        await session.execute(
            select(opened_col, func.count(OpsTicket.id))
            .where(func.date(OpsTicket.created_at) >= cutoff)
            .group_by(opened_col)
        )
    ).all()
    resolved_col = func.date(OpsTicket.resolved_at).label("bucket_r")
    resolved_rows = (
        await session.execute(
            select(resolved_col, func.count(OpsTicket.id))
            .where(OpsTicket.resolved_at.isnot(None))
            .where(func.date(OpsTicket.resolved_at) >= cutoff)
            .group_by(resolved_col)
        )
    ).all()

    by_day: dict[date, dict[str, int]] = {
        d: {"opened": 0, "resolved": 0} for d in _zero_filled_dates(days)
    }
    for bucket, count in opened_rows:
        d = bucket.date() if isinstance(bucket, datetime) else bucket
        if d in by_day:
            by_day[d]["opened"] = count
    for bucket, count in resolved_rows:
        d = bucket.date() if isinstance(bucket, datetime) else bucket
        if d in by_day:
            by_day[d]["resolved"] = count

    return [
        {"date": d.isoformat(), **c} for d, c in sorted(by_day.items())
    ]


async def analytics_timeseries(
    session: AsyncSession, *, days: int = 30
) -> dict[str, list[dict]]:
    """All four daily-bucket series in one call so the analytics page
    fetches once."""
    if days < 1:
        days = 1
    return {
        "window_days": days,
        "adherence": await daily_adherence_buckets(session, days=days),
        "inbox": await daily_inbox_buckets(session, days=days),
        "recap": await daily_recap_buckets(session, days=days),
        "tickets": await daily_ticket_buckets(session, days=days),
    }

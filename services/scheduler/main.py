from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ScheduledEvent
from app.db.repositories import scheduled_events as scheduled_events_repo
from app.db.repositories import service_heartbeats as service_heartbeats_repo
from app.db.session import get_session, get_sessionmaker
from services.scheduler import (
    adherence_pattern_sweep,
    calendar_sync_sweep,
    care_gaps,
    clinical_alert_repage_sweep,
    delivery_alert_sweep,
    delivery_reconcile_sweep,
    delivery_template_alert_sweep,
    dose_reminders,
    goal_drift_sweep,
    lab_followups,
    missed_doses,
    post_op_checklist,
    postpartum_milestones,
    pregnancy_milestones,
    recap_sweeps,
    refill_reminders,
    service_health_reconciler,
    sla_breach_sweep,
    weekly_trend_sweep,
)
from services.scheduler.dispatcher import dispatch

log = logging.getLogger(__name__)


def _poll_seconds() -> float:
    try:
        return float(os.getenv("SCHEDULER_POLL_SECONDS", "60"))
    except ValueError:
        return 60.0


def _scheduler_enabled() -> bool:
    return os.getenv("SCHEDULER_ENABLED", "1") != "0"


_SKIPPED_PREFIXES: tuple[str, ...] = ("unmapped:", "stale:", "not_applicable:")


def _dose_materialize_seconds() -> float:
    try:
        return float(os.getenv("DOSE_MATERIALIZE_SECONDS", "600"))
    except ValueError:
        return 600.0


def _missed_dose_sweep_seconds() -> float:
    try:
        return float(os.getenv("MISSED_DOSE_SWEEP_SECONDS", "300"))
    except ValueError:
        return 300.0


def _recap_sweep_seconds() -> float:
    try:
        return float(os.getenv("RECAP_SWEEP_SECONDS", "600"))
    except ValueError:
        return 600.0


def _care_gap_sweep_seconds() -> float:
    """Care-gap rules turn over slowly (180-day cadences). A 6-hour
    period is plenty — anything more frequent just burns DB cycles."""
    try:
        return float(os.getenv("CARE_GAP_SWEEP_SECONDS", "21600"))
    except ValueError:
        return 21600.0


def _service_health_reconcile_seconds() -> float:
    """How often the observability reconciler runs. Default 5 min —
    fast enough that a stalled component opens a ticket within one
    SLA-of-an-SLA, but slow enough that it doesn't drown the DB."""
    try:
        return float(os.getenv("SERVICE_HEALTH_RECONCILE_SECONDS", "300"))
    except ValueError:
        return 300.0


def _sla_breach_sweep_seconds() -> float:
    """How often the SLA breach sweep runs. 10 min default — the
    smallest ticket SLA we currently configure is 30 minutes, so a
    10-min cadence catches any first-cross within a third of the
    shortest window. Long-tail breach detection (24h SLAs etc.) is
    obviously fine at this cadence too."""
    try:
        return float(os.getenv("SLA_BREACH_SWEEP_SECONDS", "600"))
    except ValueError:
        return 600.0


def _delivery_alert_sweep_seconds() -> float:
    """How often the delivery-failure-burst sweep runs. 10 min
    default — same cadence as the SLA breach sweep. Faster cadence
    than that doesn't help; the 30-minute statistical window inside
    the sweep is the binding constraint on detection speed."""
    try:
        return float(os.getenv("DELIVERY_ALERT_SWEEP_SECONDS", "600"))
    except ValueError:
        return 600.0


def _delivery_reconcile_sweep_seconds() -> float:
    """How often the per-patient delivery reconciliation sweep runs. 1h
    default — an unreachable patient is detected against a multi-day window,
    so checking hourly is plenty and keeps the grouped status scan cheap."""
    try:
        return float(os.getenv("DELIVERY_RECONCILE_SWEEP_SECONDS", "3600"))
    except ValueError:
        return 3600.0


def _delivery_template_alert_sweep_seconds() -> float:
    """How often the per-template alert sweep runs. Same 10-min
    default as the aggregate delivery sweep — both are bounded by
    the same 30-minute statistical window. Could run faster than
    the aggregate (it's a single grouped query) but no point;
    detection speed is window-limited."""
    try:
        return float(
            os.getenv("DELIVERY_TEMPLATE_ALERT_SWEEP_SECONDS", "600")
        )
    except ValueError:
        return 600.0


def _adherence_pattern_sweep_seconds() -> float:
    """How often the adherence-drop sweep runs. 4 hours by default
    — adherence rates are 7-day rolling, so checking more often
    than every few hours just produces redundant work. The signal
    moves in days, not minutes."""
    try:
        return float(
            os.getenv("ADHERENCE_PATTERN_SWEEP_SECONDS", "14400")
        )
    except ValueError:
        return 14400.0


def _clinical_alert_repage_sweep_seconds() -> float:
    """How often the critical-alert re-page sweep runs.
    60s default — the re-page interval (5 min) plus the cap
    (3 attempts) puts the worst-case escalation window at
    roughly 15 min. Faster sweep doesn't materially shorten
    that; slower lets the first re-page slip past 5 min by a
    full poll interval."""
    try:
        return float(
            os.getenv("CLINICAL_ALERT_REPAGE_SWEEP_SECONDS", "60")
        )
    except ValueError:
        return 60.0


def _goal_drift_sweep_seconds() -> float:
    """How often the care-plan-goal drift sweep runs. 6h
    default — slipping HbA1c isn't acute, and the typical
    observation cadence is every few days, so 6h gives ops
    timely visibility without churn. Lower this if a
    deployment wants per-hour responsiveness; higher when the
    set of active goals grows past a few hundred and the
    sweep cost matters."""
    try:
        return float(
            os.getenv("GOAL_DRIFT_SWEEP_SECONDS", "21600")
        )
    except ValueError:
        return 21600.0


def _weekly_trend_push_enabled() -> bool:
    """Proactive weekly-trend pushes are opt-in (an extra outbound). Off by
    default; enable per-deployment with ``WEEKLY_TREND_PUSH_ENABLED=1``."""
    return os.getenv("WEEKLY_TREND_PUSH_ENABLED", "0") == "1"


def _weekly_trend_sweep_seconds() -> float:
    """How often the weekly-trend sweep runs. Daily default — the sweep
    dedupes to once per 7-day window per patient, so a daily cadence just
    catches each patient's window boundary promptly."""
    try:
        return float(os.getenv("WEEKLY_TREND_SWEEP_SECONDS", "86400"))
    except ValueError:
        return 86400.0


def _calendar_sync_sweep_seconds() -> float:
    """How often the inbound calendar sync runs. 10 min default.
    Bound by Google's freshness for the doctor's edits to land in
    our DB; tighter cadence burns API quota with diminishing
    returns. Future v2 swaps to webhook push notifications for
    sub-minute latency."""
    try:
        return float(
            os.getenv("CALENDAR_SYNC_SWEEP_SECONDS", "600")
        )
    except ValueError:
        return 600.0


async def _record_heartbeat(
    component: str, *, outcome: str, details: dict[str, Any] | None = None
) -> None:
    """Persist a heartbeat for ``component``. Wrapped — heartbeat
    writes must NEVER block the loop they're observing."""
    SessionLocal = get_sessionmaker()
    try:
        async with SessionLocal() as db:
            await service_heartbeats_repo.record(
                db,
                component=component,
                outcome=outcome,
                details=details or {},
            )
            await db.commit()
    except Exception:  # noqa: BLE001 — heartbeats are best-effort
        log.exception("heartbeat write failed for %s; continuing", component)


# ---- per-sweep circuit breakers --------------------------------------------
# Each sweep loop guards its runner with a process-local CircuitBreaker. When
# a sweep raises ``SWEEP_BREAKER_THRESHOLD`` consecutive exceptions the
# breaker opens for ``SWEEP_BREAKER_RESET_SECONDS``; the loop continues
# heartbeating ``circuit_open`` so the service-health reconciler can surface
# the paused state, but skips the actual runner call until the reset window
# elapses (then half-opens for a probe). Prevents a deterministically-broken
# sweep from spamming logs / DLQ for hours.
_SWEEP_BREAKERS: dict[str, "Any"] = {}


def _sweep_breaker(name: str):
    from app.circuit_breaker import CircuitBreaker

    cb = _SWEEP_BREAKERS.get(name)
    if cb is None:
        cb = CircuitBreaker(
            name=name,
            threshold=int(os.getenv("SWEEP_BREAKER_THRESHOLD", "5")),
            reset_after_seconds=float(
                os.getenv("SWEEP_BREAKER_RESET_SECONDS", "600")
            ),
        )
        _SWEEP_BREAKERS[name] = cb
    return cb


def _heartbeat_details_safe(value: Any) -> dict[str, Any]:
    """Normalise heartbeat details to a JSON-friendly dict. Some loops
    return nested dicts (recap sweep) and some return flat counters
    (missed-dose sweep) — both pass through unchanged. Non-dicts
    become {"result": <repr>} so the column still stores something
    inspectable."""
    if isinstance(value, dict):
        return value
    return {"result": repr(value)[:500]}


async def _run_dose_materialize_once() -> dict[str, Any]:
    """One pass of the dose-reminder + refill-reminder + lab-followup
    materializers. All three share the same loop cadence — they're cheap
    DB scans on regimens / lab_followups.

    Each materializer is wrapped in its own try-rollback so a transient
    failure in one (e.g. a malformed query) doesn't poison the session
    and break the others on the same tick."""
    SessionLocal = get_sessionmaker()
    out: dict[str, Any] = {}
    async with SessionLocal() as db:
        for label, runner in (
            ("dose", dose_reminders.materialize_for_all_active_regimens),
            ("refill", refill_reminders.materialize_for_all_active_regimens),
            ("lab", lab_followups.materialize_for_all_open),
            ("pregnancy", pregnancy_milestones.materialize_for_all_active),
            ("postpartum", postpartum_milestones.materialize_for_all_active),
            ("post_op", post_op_checklist.materialize_for_all_active),
        ):
            try:
                out.update(await runner(db))
                await db.commit()
            except Exception:  # noqa: BLE001 — one materializer's failure
                # shouldn't kill the others. Rollback resets the session
                # so the next runner gets a clean transaction.
                log.exception("%s materializer failed; rolling back", label)
                await db.rollback()
    return out


async def _run_missed_dose_sweep_once() -> dict[str, Any]:
    """One pass: mark past-grace adherence events as missed and escalate
    consecutive-miss streaks to ops_tickets."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        out = await missed_doses.sweep_and_escalate(db)
        await db.commit()
    return out


async def _run_recap_sweep_once() -> dict[str, Any]:
    """One pass: open ops tickets for appointments with missing recaps,
    and queue ack-nudge events for sent-but-unacked recaps."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        out = await recap_sweeps.sweep_recap_lifecycle(db)
        await db.commit()
    return out


async def _run_care_gap_sweep_once() -> dict[str, Any]:
    """One pass: materialise lab_followups for every cohort patient
    overdue for a standing care-plan test."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        out = await care_gaps.sweep_care_gaps(db)
        await db.commit()
    return out


async def _run_service_health_reconcile_once() -> dict[str, Any]:
    """One pass: scan heartbeats + scheduled_events and open idempotent
    ops_tickets for stale components, errored components, and failed-
    event bursts."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        out = await service_health_reconciler.reconcile_service_health(db)
        await db.commit()
    return out


async def _run_sla_breach_sweep_once() -> dict[str, Any]:
    """One pass: stamp sla_breached_at on tickets that have crossed
    their SLA window without being resolved or snoozed."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        out = await sla_breach_sweep.sweep_sla_breaches(db)
        await db.commit()
    return out


async def _run_clinical_alert_repage_sweep_once() -> dict[str, Any]:
    """One pass: enqueue paging events for unacked critical
    alerts that have crossed the re-page threshold."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        out = await clinical_alert_repage_sweep.sweep_repages(db)
        await db.commit()
    return out


async def _run_goal_drift_sweep_once() -> dict[str, Any]:
    """One pass: classify every active care-plan goal's trend
    and open / auto-resolve ops tickets for the alert-worthy
    states (slipping / persistent_off / stale)."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        out = await goal_drift_sweep.sweep_goal_drift(db)
        await db.commit()
    return out


async def _run_weekly_trend_sweep_once() -> dict[str, Any]:
    """One pass: enqueue a weekly self-report trend push for each patient
    with recent readings (deduped to once per 7-day window)."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        out = await weekly_trend_sweep.sweep_weekly_trends(db)
        await db.commit()
    return out


async def _run_delivery_alert_sweep_once() -> dict[str, Any]:
    """One pass: open / re-note / auto-resolve a single platform-level
    ops ticket based on the rolling outbound-delivery failure rate."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        out = await delivery_alert_sweep.sweep_delivery_alerts(db)
        await db.commit()
    return out


async def _run_delivery_reconcile_sweep_once() -> dict[str, Any]:
    """One pass: open ``patient_unreachable`` tickets for silent patients
    (persistent per-patient delivery failures) and auto-resolve ones who
    are reachable again."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        out = await delivery_reconcile_sweep.sweep_unreachable_patients(db)
        await db.commit()
    return out


async def _run_delivery_template_alert_sweep_once() -> dict[str, Any]:
    """One pass: walk the per-template delivery rollup, opening /
    re-noting / auto-resolving a ticket per template based on its
    rolling failure rate. Catches single-template failures the
    aggregate sweep would average away."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        out = await delivery_template_alert_sweep.sweep_template_alerts(db)
        await db.commit()
    return out


async def _run_adherence_pattern_sweep_once() -> dict[str, Any]:
    """One pass: walk patients with recent adherence events,
    detect rate drops below threshold, open / re-note / auto-
    resolve per-patient ``adherence_drop`` tickets. Surfaces
    quietly-non-adherent patients to the doctor's daily digest."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        out = await adherence_pattern_sweep.sweep_adherence_drops(db)
        await db.commit()
    return out


async def _run_calendar_sync_sweep_once() -> dict[str, Any]:
    """One pass: poll Google Calendar for changes per doctor,
    reconcile cancelled / rescheduled events back to our
    appointments table."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        out = await calendar_sync_sweep.sweep_calendar_changes(db)
        await db.commit()
    return out


async def _dose_materialize_loop(stop_event: asyncio.Event, interval: float) -> None:
    log.info(
        "dose+refill materialize loop started (interval=%ss)", interval
    )
    while not stop_event.is_set():
        breaker = _sweep_breaker("scheduler.dose_materialize")
        if breaker.is_open():
            await _record_heartbeat(
                "scheduler.dose_materialize",
                outcome="circuit_open",
                details={"breaker": breaker.snapshot()},
            )
        else:
            try:
                result = await _run_dose_materialize_once()
                if (
                    result.get("new_dose_events", 0) > 0
                    or result.get("new_refill_events", 0) > 0
                    or result.get("new_lab_followup_events", 0) > 0
                ):
                    log.info("materializer pass: %s", result)
                await _record_heartbeat(
                    "scheduler.dose_materialize",
                    outcome="ok",
                    details=_heartbeat_details_safe(result),
                )
                breaker.record_success()
            except Exception:  # noqa: BLE001 — keep loop alive
                log.exception("dose+refill materialize tick raised; continuing")
                await _record_heartbeat(
                    "scheduler.dose_materialize", outcome="error"
                )
                breaker.record_failure()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    log.info("dose+refill materialize loop stopped")


async def _missed_dose_sweep_loop(
    stop_event: asyncio.Event, interval: float
) -> None:
    log.info("missed-dose sweep loop started (interval=%ss)", interval)
    while not stop_event.is_set():
        breaker = _sweep_breaker("scheduler.missed_dose_sweep")
        if breaker.is_open():
            await _record_heartbeat(
                "scheduler.missed_dose_sweep",
                outcome="circuit_open",
                details={"breaker": breaker.snapshot()},
            )
        else:
            try:
                result = await _run_missed_dose_sweep_once()
                if result.get("marked_missed", 0) > 0 or result.get("escalated", 0) > 0:
                    log.info("missed-dose sweep: %s", result)
                await _record_heartbeat(
                    "scheduler.missed_dose_sweep",
                    outcome="ok",
                    details=_heartbeat_details_safe(result),
                )
                breaker.record_success()
            except Exception:  # noqa: BLE001 — keep loop alive
                log.exception("missed-dose sweep raised; continuing")
                await _record_heartbeat(
                    "scheduler.missed_dose_sweep", outcome="error"
                )
                breaker.record_failure()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    log.info("missed-dose sweep loop stopped")


async def _recap_sweep_loop(
    stop_event: asyncio.Event, interval: float
) -> None:
    log.info("recap sweep loop started (interval=%ss)", interval)
    while not stop_event.is_set():
        breaker = _sweep_breaker("scheduler.recap_sweep")
        if breaker.is_open():
            await _record_heartbeat(
                "scheduler.recap_sweep",
                outcome="circuit_open",
                details={"breaker": breaker.snapshot()},
            )
        else:
            try:
                result = await _run_recap_sweep_once()
                if (
                    result.get("missing", {}).get("opened", 0) > 0
                    or result.get("unacked", {}).get("enqueued", 0) > 0
                ):
                    log.info("recap sweep: %s", result)
                await _record_heartbeat(
                    "scheduler.recap_sweep",
                    outcome="ok",
                    details=_heartbeat_details_safe(result),
                )
                breaker.record_success()
            except Exception:  # noqa: BLE001 — keep loop alive
                log.exception("recap sweep raised; continuing")
                await _record_heartbeat(
                    "scheduler.recap_sweep", outcome="error"
                )
                breaker.record_failure()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    log.info("recap sweep loop stopped")


async def _service_health_reconcile_loop(
    stop_event: asyncio.Event, interval: float
) -> None:
    log.info(
        "service-health reconciler loop started (interval=%ss)", interval
    )
    while not stop_event.is_set():
        breaker = _sweep_breaker("scheduler.service_health_reconcile")
        if breaker.is_open():
            await _record_heartbeat(
                "scheduler.service_health_reconcile",
                outcome="circuit_open",
                details={"breaker": breaker.snapshot()},
            )
        else:
            try:
                result = await _run_service_health_reconcile_once()
                if (
                    result.get("stale_opened", 0) > 0
                    or result.get("errored_opened", 0) > 0
                    or result.get("failed_burst_opened", 0) > 0
                ):
                    log.warning("service-health reconciler opened tickets: %s", result)
                await _record_heartbeat(
                    "scheduler.service_health_reconcile",
                    outcome="ok",
                    details=_heartbeat_details_safe(result),
                )
                breaker.record_success()
            except Exception:  # noqa: BLE001 — keep loop alive
                log.exception("service-health reconciler raised; continuing")
                await _record_heartbeat(
                    "scheduler.service_health_reconcile", outcome="error"
                )
                breaker.record_failure()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    log.info("service-health reconciler loop stopped")


async def _sla_breach_sweep_loop(
    stop_event: asyncio.Event, interval: float
) -> None:
    log.info("SLA breach sweep loop started (interval=%ss)", interval)
    while not stop_event.is_set():
        breaker = _sweep_breaker("scheduler.sla_breach_sweep")
        if breaker.is_open():
            await _record_heartbeat(
                "scheduler.sla_breach_sweep",
                outcome="circuit_open",
                details={"breaker": breaker.snapshot()},
            )
        else:
            try:
                result = await _run_sla_breach_sweep_once()
                if result.get("breached", 0) > 0:
                    log.warning(
                        "SLA breach sweep stamped %d ticket(s): %s",
                        result["breached"],
                        result,
                    )
                await _record_heartbeat(
                    "scheduler.sla_breach_sweep",
                    outcome="ok",
                    details=_heartbeat_details_safe(result),
                )
                breaker.record_success()
            except Exception:  # noqa: BLE001 — keep loop alive
                log.exception("SLA breach sweep raised; continuing")
                await _record_heartbeat(
                    "scheduler.sla_breach_sweep", outcome="error"
                )
                breaker.record_failure()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    log.info("SLA breach sweep loop stopped")


async def _goal_drift_sweep_loop(
    stop_event: asyncio.Event, interval: float
) -> None:
    log.info(
        "goal-drift sweep loop started (interval=%ss)", interval
    )
    while not stop_event.is_set():
        breaker = _sweep_breaker("scheduler.goal_drift_sweep")
        if breaker.is_open():
            await _record_heartbeat(
                "scheduler.goal_drift_sweep",
                outcome="circuit_open",
                details={"breaker": breaker.snapshot()},
            )
        else:
            try:
                result = await _run_goal_drift_sweep_once()
                if (
                    result.get("opened", 0) > 0
                    or result.get("resolved", 0) > 0
                ):
                    log.info(
                        "goal drift sweep: %d opened, %d resolved (%s)",
                        result["opened"],
                        result["resolved"],
                        result.get("by_drift", {}),
                    )
                await _record_heartbeat(
                    "scheduler.goal_drift_sweep",
                    outcome="ok",
                    details=_heartbeat_details_safe(result),
                )
                breaker.record_success()
            except Exception:  # noqa: BLE001 — keep loop alive
                log.exception("goal-drift sweep raised; continuing")
                await _record_heartbeat(
                    "scheduler.goal_drift_sweep", outcome="error"
                )
                breaker.record_failure()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    log.info("goal-drift sweep loop stopped")


async def _weekly_trend_sweep_loop(
    stop_event: asyncio.Event, interval: float
) -> None:
    log.info(
        "weekly-trend sweep loop started (interval=%ss)", interval
    )
    while not stop_event.is_set():
        breaker = _sweep_breaker("scheduler.weekly_trend_sweep")
        if breaker.is_open():
            await _record_heartbeat(
                "scheduler.weekly_trend_sweep",
                outcome="circuit_open",
                details={"breaker": breaker.snapshot()},
            )
        else:
            try:
                result = await _run_weekly_trend_sweep_once()
                await _record_heartbeat(
                    "scheduler.weekly_trend_sweep",
                    outcome="ok",
                    details=_heartbeat_details_safe(result),
                )
                breaker.record_success()
            except Exception:  # noqa: BLE001 — keep loop alive
                log.exception("weekly-trend sweep raised; continuing")
                await _record_heartbeat(
                    "scheduler.weekly_trend_sweep", outcome="error"
                )
                breaker.record_failure()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    log.info("weekly-trend sweep loop stopped")


async def _clinical_alert_repage_sweep_loop(
    stop_event: asyncio.Event, interval: float
) -> None:
    log.info(
        "clinical-alert re-page sweep loop started (interval=%ss)",
        interval,
    )
    while not stop_event.is_set():
        breaker = _sweep_breaker("scheduler.clinical_alert_repage_sweep")
        if breaker.is_open():
            await _record_heartbeat(
                "scheduler.clinical_alert_repage_sweep",
                outcome="circuit_open",
                details={"breaker": breaker.snapshot()},
            )
        else:
            try:
                result = await _run_clinical_alert_repage_sweep_once()
                if result.get("repaged", 0) > 0:
                    log.warning(
                        "clinical-alert re-page sweep enqueued %d page(s): %s",
                        result["repaged"],
                        result,
                    )
                await _record_heartbeat(
                    "scheduler.clinical_alert_repage_sweep",
                    outcome="ok",
                    details=_heartbeat_details_safe(result),
                )
                breaker.record_success()
            except Exception:  # noqa: BLE001 — keep loop alive
                log.exception(
                    "clinical-alert re-page sweep raised; continuing"
                )
                await _record_heartbeat(
                    "scheduler.clinical_alert_repage_sweep",
                    outcome="error",
                )
                breaker.record_failure()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    log.info("clinical-alert re-page sweep loop stopped")


async def _delivery_alert_sweep_loop(
    stop_event: asyncio.Event, interval: float
) -> None:
    log.info(
        "delivery alert sweep loop started (interval=%ss)", interval
    )
    while not stop_event.is_set():
        breaker = _sweep_breaker("scheduler.delivery_alert_sweep")
        if breaker.is_open():
            await _record_heartbeat(
                "scheduler.delivery_alert_sweep",
                outcome="circuit_open",
                details={"breaker": breaker.snapshot()},
            )
        else:
            try:
                result = await _run_delivery_alert_sweep_once()
                if result.get("opened") or result.get("auto_resolved"):
                    log.warning(
                        "delivery alert sweep state change: %s", result
                    )
                await _record_heartbeat(
                    "scheduler.delivery_alert_sweep",
                    outcome="ok",
                    details=_heartbeat_details_safe(result),
                )
                breaker.record_success()
            except Exception:  # noqa: BLE001 — keep loop alive
                log.exception("delivery alert sweep raised; continuing")
                await _record_heartbeat(
                    "scheduler.delivery_alert_sweep", outcome="error"
                )
                breaker.record_failure()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    log.info("delivery alert sweep loop stopped")


async def _delivery_reconcile_sweep_loop(
    stop_event: asyncio.Event, interval: float
) -> None:
    log.info(
        "delivery reconcile sweep loop started (interval=%ss)", interval
    )
    while not stop_event.is_set():
        breaker = _sweep_breaker("scheduler.delivery_reconcile_sweep")
        if breaker.is_open():
            await _record_heartbeat(
                "scheduler.delivery_reconcile_sweep",
                outcome="circuit_open",
                details={"breaker": breaker.snapshot()},
            )
        else:
            try:
                result = await _run_delivery_reconcile_sweep_once()
                if result.get("opened") or result.get("auto_resolved"):
                    log.warning(
                        "delivery reconcile sweep state change: %s", result
                    )
                await _record_heartbeat(
                    "scheduler.delivery_reconcile_sweep",
                    outcome="ok",
                    details=_heartbeat_details_safe(result),
                )
                breaker.record_success()
            except Exception:  # noqa: BLE001 — keep loop alive
                log.exception("delivery reconcile sweep raised; continuing")
                await _record_heartbeat(
                    "scheduler.delivery_reconcile_sweep", outcome="error"
                )
                breaker.record_failure()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    log.info("delivery reconcile sweep loop stopped")


async def _delivery_template_alert_sweep_loop(
    stop_event: asyncio.Event, interval: float
) -> None:
    log.info(
        "delivery template alert sweep loop started "
        "(interval=%ss)", interval
    )
    while not stop_event.is_set():
        breaker = _sweep_breaker("scheduler.delivery_template_alert_sweep")
        if breaker.is_open():
            await _record_heartbeat(
                "scheduler.delivery_template_alert_sweep",
                outcome="circuit_open",
                details={"breaker": breaker.snapshot()},
            )
        else:
            try:
                result = await _run_delivery_template_alert_sweep_once()
                if result.get("opened") or result.get("auto_resolved"):
                    log.warning(
                        "delivery template alert sweep state change: %s",
                        result,
                    )
                await _record_heartbeat(
                    "scheduler.delivery_template_alert_sweep",
                    outcome="ok",
                    details=_heartbeat_details_safe(result),
                )
                breaker.record_success()
            except Exception:  # noqa: BLE001 — keep loop alive
                log.exception(
                    "delivery template alert sweep raised; continuing"
                )
                await _record_heartbeat(
                    "scheduler.delivery_template_alert_sweep",
                    outcome="error",
                )
                breaker.record_failure()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    log.info("delivery template alert sweep loop stopped")


async def _adherence_pattern_sweep_loop(
    stop_event: asyncio.Event, interval: float
) -> None:
    log.info(
        "adherence pattern sweep loop started (interval=%ss)",
        interval,
    )
    while not stop_event.is_set():
        breaker = _sweep_breaker("scheduler.adherence_pattern_sweep")
        if breaker.is_open():
            await _record_heartbeat(
                "scheduler.adherence_pattern_sweep",
                outcome="circuit_open",
                details={"breaker": breaker.snapshot()},
            )
        else:
            try:
                result = await _run_adherence_pattern_sweep_once()
                if result.get("opened") or result.get("auto_resolved"):
                    log.warning(
                        "adherence pattern sweep state change: %s",
                        result,
                    )
                await _record_heartbeat(
                    "scheduler.adherence_pattern_sweep",
                    outcome="ok",
                    details=_heartbeat_details_safe(result),
                )
                breaker.record_success()
            except Exception:  # noqa: BLE001 — keep loop alive
                log.exception(
                    "adherence pattern sweep raised; continuing"
                )
                await _record_heartbeat(
                    "scheduler.adherence_pattern_sweep",
                    outcome="error",
                )
                breaker.record_failure()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    log.info("adherence pattern sweep loop stopped")


async def _calendar_sync_sweep_loop(
    stop_event: asyncio.Event, interval: float
) -> None:
    log.info(
        "calendar sync sweep loop started (interval=%ss)",
        interval,
    )
    while not stop_event.is_set():
        breaker = _sweep_breaker("scheduler.calendar_sync_sweep")
        if breaker.is_open():
            await _record_heartbeat(
                "scheduler.calendar_sync_sweep",
                outcome="circuit_open",
                details={"breaker": breaker.snapshot()},
            )
        else:
            try:
                result = await _run_calendar_sync_sweep_once()
                totals = result.get("totals", {}) or {}
                if (
                    totals.get("cancelled", 0)
                    or totals.get("rescheduled", 0)
                    or totals.get("errors", 0)
                ):
                    log.warning(
                        "calendar sync sweep state change: %s",
                        result,
                    )
                await _record_heartbeat(
                    "scheduler.calendar_sync_sweep",
                    outcome="ok",
                    details=_heartbeat_details_safe(result),
                )
                breaker.record_success()
            except Exception:  # noqa: BLE001 — keep loop alive
                log.exception(
                    "calendar sync sweep raised; continuing"
                )
                await _record_heartbeat(
                    "scheduler.calendar_sync_sweep",
                    outcome="error",
                )
                breaker.record_failure()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    log.info("calendar sync sweep loop stopped")


async def _care_gap_sweep_loop(
    stop_event: asyncio.Event, interval: float
) -> None:
    log.info("care-gap sweep loop started (interval=%ss)", interval)
    while not stop_event.is_set():
        breaker = _sweep_breaker("scheduler.care_gap_sweep")
        if breaker.is_open():
            await _record_heartbeat(
                "scheduler.care_gap_sweep",
                outcome="circuit_open",
                details={"breaker": breaker.snapshot()},
            )
        else:
            try:
                result = await _run_care_gap_sweep_once()
                total_materialized = sum(
                    (counts or {}).get("materialized", 0)
                    for counts in result.values()
                )
                if total_materialized > 0:
                    log.info("care-gap sweep: %s", result)
                await _record_heartbeat(
                    "scheduler.care_gap_sweep",
                    outcome="ok",
                    details=_heartbeat_details_safe(result),
                )
                breaker.record_success()
            except Exception:  # noqa: BLE001 — keep loop alive
                log.exception("care-gap sweep raised; continuing")
                await _record_heartbeat(
                    "scheduler.care_gap_sweep", outcome="error"
                )
                breaker.record_failure()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    log.info("care-gap sweep loop stopped")


async def _run_tick_once(*, gateway_url: str | None = None) -> dict[str, Any]:
    """One async poll cycle: claim due events, dispatch, mark each.

    Commits PER EVENT, not once at the end. ``dispatch`` performs the outbound
    side effect (the WhatsApp send) before we mark the row; a single commit at
    the end meant that if any later event raised (a DB blip, a bad row), the
    whole transaction rolled back — un-marking events we'd ALREADY sent, so the
    next tick re-dispatched them and the patient got duplicate messages. A
    per-event commit + per-event rollback bounds the blast radius to one row.
    """
    SessionLocal = get_sessionmaker()
    dispatched = 0
    failed = 0
    skipped = 0
    errored = 0

    async with SessionLocal() as db:
        due = await scheduled_events_repo.fetch_due(db)
        for event in due:
            try:
                err = await dispatch(event, db=db, gateway_url=gateway_url)
                if err is None:
                    await scheduled_events_repo.mark_dispatched(db, event.id)
                    dispatched += 1
                elif any(err.startswith(p) for p in _SKIPPED_PREFIXES):
                    await scheduled_events_repo.mark_skipped(
                        db, event.id, reason=err
                    )
                    skipped += 1
                else:
                    # Permanent failures (4xx from the gateway) skip the retry
                    # backoff and go straight to the DLQ — retrying won't help.
                    await scheduled_events_repo.mark_failed(
                        db,
                        event.id,
                        error=err,
                        permanent=err.startswith("http_error_permanent:"),
                    )
                    failed += 1
                # Persist THIS event's send + mark before touching the next one
                # so a later failure can't roll back an already-sent message.
                await db.commit()
            except Exception:  # noqa: BLE001 — isolate one bad row, keep ticking
                log.exception(
                    "scheduled event %s: dispatch/mark failed; rolling back "
                    "this event and continuing",
                    event.id,
                )
                await db.rollback()
                errored += 1

    return {
        "dispatched": dispatched,
        "failed": failed,
        "skipped": skipped,
        "errored": errored,
        "examined": len(due),
    }


async def _polling_loop(stop_event: asyncio.Event, interval: float) -> None:
    log.info("scheduler polling loop started (interval=%ss)", interval)
    while not stop_event.is_set():
        try:
            result = await _run_tick_once()
            await _record_heartbeat(
                "scheduler.dispatch",
                outcome="ok",
                details=_heartbeat_details_safe(result),
            )
        except Exception:  # noqa: BLE001 — keep the loop alive
            log.exception("scheduler tick raised; continuing")
            await _record_heartbeat("scheduler.dispatch", outcome="error")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    log.info("scheduler polling loop stopped")


@asynccontextmanager
async def lifespan(app: FastAPI):
    stop_event = asyncio.Event()
    dispatch_task: asyncio.Task[None] | None = None
    materialize_task: asyncio.Task[None] | None = None
    sweep_task: asyncio.Task[None] | None = None
    recap_sweep_task: asyncio.Task[None] | None = None
    care_gap_sweep_task: asyncio.Task[None] | None = None
    health_reconcile_task: asyncio.Task[None] | None = None
    sla_breach_task: asyncio.Task[None] | None = None
    delivery_alert_task: asyncio.Task[None] | None = None
    delivery_reconcile_task: asyncio.Task[None] | None = None
    delivery_template_alert_task: asyncio.Task[None] | None = None
    adherence_pattern_task: asyncio.Task[None] | None = None
    calendar_sync_task: asyncio.Task[None] | None = None
    clinical_alert_repage_task: asyncio.Task[None] | None = None
    goal_drift_task: asyncio.Task[None] | None = None
    weekly_trend_task: asyncio.Task[None] | None = None
    if _scheduler_enabled() and os.getenv("DATABASE_URL"):
        dispatch_task = asyncio.create_task(
            _polling_loop(stop_event, _poll_seconds())
        )
        materialize_task = asyncio.create_task(
            _dose_materialize_loop(stop_event, _dose_materialize_seconds())
        )
        sweep_task = asyncio.create_task(
            _missed_dose_sweep_loop(stop_event, _missed_dose_sweep_seconds())
        )
        recap_sweep_task = asyncio.create_task(
            _recap_sweep_loop(stop_event, _recap_sweep_seconds())
        )
        care_gap_sweep_task = asyncio.create_task(
            _care_gap_sweep_loop(stop_event, _care_gap_sweep_seconds())
        )
        health_reconcile_task = asyncio.create_task(
            _service_health_reconcile_loop(
                stop_event, _service_health_reconcile_seconds()
            )
        )
        sla_breach_task = asyncio.create_task(
            _sla_breach_sweep_loop(stop_event, _sla_breach_sweep_seconds())
        )
        delivery_alert_task = asyncio.create_task(
            _delivery_alert_sweep_loop(
                stop_event, _delivery_alert_sweep_seconds()
            )
        )
        delivery_reconcile_task = asyncio.create_task(
            _delivery_reconcile_sweep_loop(
                stop_event, _delivery_reconcile_sweep_seconds()
            )
        )
        delivery_template_alert_task = asyncio.create_task(
            _delivery_template_alert_sweep_loop(
                stop_event,
                _delivery_template_alert_sweep_seconds(),
            )
        )
        adherence_pattern_task = asyncio.create_task(
            _adherence_pattern_sweep_loop(
                stop_event,
                _adherence_pattern_sweep_seconds(),
            )
        )
        calendar_sync_task = asyncio.create_task(
            _calendar_sync_sweep_loop(
                stop_event,
                _calendar_sync_sweep_seconds(),
            )
        )
        clinical_alert_repage_task = asyncio.create_task(
            _clinical_alert_repage_sweep_loop(
                stop_event,
                _clinical_alert_repage_sweep_seconds(),
            )
        )
        goal_drift_task = asyncio.create_task(
            _goal_drift_sweep_loop(
                stop_event,
                _goal_drift_sweep_seconds(),
            )
        )
        if _weekly_trend_push_enabled():
            weekly_trend_task = asyncio.create_task(
                _weekly_trend_sweep_loop(
                    stop_event,
                    _weekly_trend_sweep_seconds(),
                )
            )
    else:
        log.info("scheduler loops disabled (SCHEDULER_ENABLED=%s)", os.getenv("SCHEDULER_ENABLED"))
    app.state.scheduler_stop = stop_event
    app.state.scheduler_task = dispatch_task
    app.state.dose_materialize_task = materialize_task
    app.state.missed_dose_sweep_task = sweep_task
    app.state.recap_sweep_task = recap_sweep_task
    app.state.care_gap_sweep_task = care_gap_sweep_task
    app.state.health_reconcile_task = health_reconcile_task
    app.state.sla_breach_task = sla_breach_task
    app.state.delivery_alert_task = delivery_alert_task
    app.state.delivery_reconcile_task = delivery_reconcile_task
    app.state.delivery_template_alert_task = delivery_template_alert_task
    app.state.adherence_pattern_task = adherence_pattern_task
    app.state.calendar_sync_task = calendar_sync_task
    app.state.clinical_alert_repage_task = clinical_alert_repage_task
    app.state.goal_drift_task = goal_drift_task
    app.state.weekly_trend_task = weekly_trend_task
    try:
        yield
    finally:
        stop_event.set()
        for t in (
            dispatch_task,
            materialize_task,
            sweep_task,
            recap_sweep_task,
            care_gap_sweep_task,
            health_reconcile_task,
            sla_breach_task,
            delivery_alert_task,
            delivery_reconcile_task,
            delivery_template_alert_task,
            adherence_pattern_task,
            calendar_sync_task,
            clinical_alert_repage_task,
            goal_drift_task,
            weekly_trend_task,
        ):
            if t is None:
                continue
            try:
                await asyncio.wait_for(t, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                t.cancel()


app = FastAPI(title="scheduler", lifespan=lifespan)


# ---- Request schemas (preserve the original /emit-* shapes) -----------------


class DoseDueRequest(BaseModel):
    patient_id: str = Field(min_length=1)
    regimen_id: str = Field(min_length=1)
    medication_name: str | None = None
    scheduled_for: datetime | None = None


class RefillDueRequest(BaseModel):
    patient_id: str = Field(min_length=1)
    medication_name: str = Field(min_length=1)
    days_left: int = Field(ge=0)
    # regimen_id is REQUIRED for the event to actually dispatch — the
    # dispatcher's refill branch (services/scheduler/dispatcher.py)
    # looks the regimen up to validate ends_on + supply cycle and to
    # build the Refilled button id. Optional in the schema only so the
    # production materializer (refill_reminders.py) — which always sets
    # it — and legacy callers don't 422; an emit without it enqueues a
    # row the dispatcher will skip as "missing regimen_id".
    regimen_id: int | None = None
    dose: str | None = None
    scheduled_for: datetime | None = None


class TriageAlertRequest(BaseModel):
    patient_id: str = Field(min_length=1)
    cohort: str = Field(min_length=1)
    severity: str = Field(pattern="^(low|medium|high|critical)$")
    reason: str = Field(min_length=3)
    scheduled_for: datetime | None = None


class FollowupClosureRequest(BaseModel):
    patient_id: str = Field(min_length=1)
    followup_type: str = Field(pattern="^(lab|appointment)$")
    item_name: str = Field(min_length=1)
    status: str = Field(pattern="^(due|booked|completed|reviewed)$")
    scheduled_for: datetime | None = None


class ScheduledEventDTO(BaseModel):
    id: int
    event_type: str
    patient_id: str
    scheduled_for: datetime
    payload: dict[str, Any]
    status: str
    created_at: datetime
    dispatched_at: datetime | None = None
    error: str | None = None


def _to_dto(row: ScheduledEvent) -> ScheduledEventDTO:
    return ScheduledEventDTO(
        id=row.id,
        event_type=row.event_type,
        patient_id=row.patient_id,
        scheduled_for=row.scheduled_for,
        payload=row.payload,
        status=row.status.value,
        created_at=row.created_at,
        dispatched_at=row.dispatched_at,
        error=row.error,
    )


def _refill_stage(days_left: int) -> str:
    return "d1" if days_left <= 1 else "d3" if days_left <= 3 else "d7"


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness — process up?"""
    return {"status": "ok"}


@app.get("/health/live")
async def health_live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
async def health_ready(response: Response) -> dict[str, object]:
    """Readiness — DB reachable + Fernet loadable. The scheduler doesn't serve
    LLM traffic, so it skips the LLM check. 503 when the DB is down so the
    deploy system knows the sweeps aren't running against a live DB."""
    from app.health import readiness_report

    ready, report = await readiness_report(check_llm=False)
    if not ready:
        response.status_code = 503
    return report


@app.post("/emit-dose-due", response_model=ScheduledEventDTO)
async def emit_dose_due(
    payload: DoseDueRequest, db: AsyncSession = Depends(get_session)
) -> ScheduledEventDTO:
    row = await scheduled_events_repo.enqueue(
        db,
        event_type="dose_due",
        patient_id=payload.patient_id,
        payload={
            "regimen_id": payload.regimen_id,
            "medication_name": payload.medication_name,
        },
        scheduled_for=payload.scheduled_for,
    )
    return _to_dto(row)


@app.post("/emit-refill-due", response_model=ScheduledEventDTO)
async def emit_refill_due(
    payload: RefillDueRequest, db: AsyncSession = Depends(get_session)
) -> ScheduledEventDTO:
    # Build a payload shaped exactly like the production materializer
    # (refill_reminders.py) so the dispatcher's refill branch can render
    # it: regimen_id (for lookup + Refilled button), medication_name,
    # dose, stage (dispatcher reads "stage", NOT "refill_stage"), and
    # days_left. cycle_key is intentionally omitted for manual emits so
    # the dispatcher skips the supply-cycle drift check.
    row = await scheduled_events_repo.enqueue(
        db,
        event_type="refill_due",
        patient_id=payload.patient_id,
        payload={
            "regimen_id": payload.regimen_id,
            "medication_name": payload.medication_name,
            "dose": payload.dose,
            "days_left": payload.days_left,
            "stage": _refill_stage(payload.days_left),
            "actions": "REORDER/UPDATE COUNT",
        },
        scheduled_for=payload.scheduled_for,
    )
    return _to_dto(row)


@app.post("/emit-triage-alert", response_model=ScheduledEventDTO)
async def emit_triage_alert(
    payload: TriageAlertRequest, db: AsyncSession = Depends(get_session)
) -> ScheduledEventDTO:
    row = await scheduled_events_repo.enqueue(
        db,
        event_type="triage_alert",
        patient_id=payload.patient_id,
        payload={
            "cohort": payload.cohort,
            "severity": payload.severity,
            "reason": payload.reason,
        },
        scheduled_for=payload.scheduled_for,
    )
    return _to_dto(row)


@app.post("/emit-followup-closure", response_model=ScheduledEventDTO)
async def emit_followup_closure(
    payload: FollowupClosureRequest, db: AsyncSession = Depends(get_session)
) -> ScheduledEventDTO:
    row = await scheduled_events_repo.enqueue(
        db,
        event_type="followup_closure",
        patient_id=payload.patient_id,
        payload={
            "followup_type": payload.followup_type,
            "item_name": payload.item_name,
            "status": payload.status,
        },
        scheduled_for=payload.scheduled_for,
    )
    return _to_dto(row)


@app.post("/scheduler/tick")
async def scheduler_tick(gateway_url: str | None = Query(default=None)) -> dict[str, Any]:
    """Force one immediate poll cycle. Returns counts."""
    return await _run_tick_once(gateway_url=gateway_url)


@app.post("/scheduler/materialize-doses")
async def scheduler_materialize_doses() -> dict[str, Any]:
    """Force one dose-materialize pass for every active regimen. Useful
    after creating a new regimen so the patient gets their next dose
    reminder without waiting for the periodic loop."""
    return await _run_dose_materialize_once()


@app.post("/scheduler/sweep-missed-doses")
async def scheduler_sweep_missed_doses() -> dict[str, Any]:
    """Force one missed-dose sweep + escalation pass. Returns counts of
    AdherenceEvents marked missed and ops_tickets created. Useful for
    demos / smoke tests."""
    return await _run_missed_dose_sweep_once()


@app.post("/scheduler/sweep-recaps")
async def scheduler_sweep_recaps() -> dict[str, Any]:
    """Force one recap-lifecycle sweep — opens ops tickets for missing
    recaps, queues ack-nudge events for sent-but-unacked recaps. Returns
    the per-pass counts."""
    return await _run_recap_sweep_once()


@app.post("/scheduler/sweep-care-gaps")
async def scheduler_sweep_care_gaps() -> dict[str, Any]:
    """Force one care-gap pass: materialise lab_followups for cohort
    patients overdue for a standing care-plan test. Returns per-test
    counts (cohort_size, materialized, skipped_open_followup,
    skipped_recent_completion)."""
    return await _run_care_gap_sweep_once()


@app.post("/scheduler/reconcile-service-health")
async def scheduler_reconcile_service_health() -> dict[str, Any]:
    """Force one observability reconciler pass: scan heartbeats +
    failed scheduled_events and open idempotent ops_tickets for
    stale/errored components or failed-event bursts."""
    return await _run_service_health_reconcile_once()


@app.get("/scheduled-events", response_model=list[ScheduledEventDTO])
async def list_scheduled_events(
    db: AsyncSession = Depends(get_session),
    status: str | None = Query(default=None),
    patient_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[ScheduledEventDTO]:
    if status is not None and status not in {"pending", "dispatched", "skipped", "failed"}:
        raise HTTPException(status_code=400, detail="invalid status filter")
    rows = await scheduled_events_repo.list_recent(
        db, status=status, patient_id=patient_id, limit=limit
    )
    return [_to_dto(row) for row in rows]

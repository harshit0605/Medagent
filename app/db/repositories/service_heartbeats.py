from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ServiceHeartbeat


# Conventional component names. The repo doesn't enforce membership —
# adding a new loop is just calling ``record(component="my.new.loop", ...)``.
KNOWN_COMPONENTS: tuple[str, ...] = (
    "scheduler.dispatch",
    "scheduler.dose_materialize",
    "scheduler.missed_dose_sweep",
    "scheduler.recap_sweep",
    "scheduler.care_gap_sweep",
)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def record(
    session: AsyncSession,
    *,
    component: str,
    outcome: str = "ok",
    details: dict | None = None,
    at: datetime | None = None,
) -> ServiceHeartbeat:
    """Upsert the heartbeat for ``component``. ``outcome`` ∈ {ok,
    error, skipped}; ``error`` increments ``consecutive_errors`` so
    the /ops/health page can highlight loops that have been failing
    in a row. Any other outcome resets the counter.

    Caller is expected to commit the session — the repo only flushes."""
    row = await session.get(ServiceHeartbeat, component)
    when = _ensure_utc(at or datetime.now(timezone.utc))
    if row is None:
        row = ServiceHeartbeat(
            component=component,
            last_run_at=when,
            last_outcome=outcome,
            details=details or {},
            consecutive_errors=1 if outcome == "error" else 0,
        )
        session.add(row)
    else:
        row.last_run_at = when
        row.last_outcome = outcome
        row.details = details or {}
        if outcome == "error":
            row.consecutive_errors = (row.consecutive_errors or 0) + 1
        else:
            row.consecutive_errors = 0
    await session.flush()
    await session.refresh(row)
    return row


async def get(
    session: AsyncSession, component: str
) -> ServiceHeartbeat | None:
    return await session.get(ServiceHeartbeat, component)


async def list_all(session: AsyncSession) -> list[ServiceHeartbeat]:
    stmt = select(ServiceHeartbeat).order_by(ServiceHeartbeat.component)
    return list((await session.execute(stmt)).scalars().all())

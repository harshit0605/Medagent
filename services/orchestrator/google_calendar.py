"""Async Google Calendar client + tool primitives for the booking agent.

Each tool below takes a ``doctor_id`` (resolves the doctor's stored OAuth
tokens, refreshing the access token if expired) and returns a typed result.
The agent layer in a later iteration will expose these as LangGraph
``Tool``s; for now they're directly callable from FastAPI handlers and tests.

Endpoints used:
- ``POST https://oauth2.googleapis.com/token``        — refresh access token
- ``POST .../calendar/v3/freeBusy``                   — availability lookup
- ``POST .../calendar/v3/calendars/{id}/events``      — create booking
- ``GET  .../calendar/v3/calendars/{id}/events/{ev}`` — read event
- ``PATCH .../calendar/v3/calendars/{id}/events/{ev}``— reschedule
- ``DELETE .../calendar/v3/calendars/{id}/events/{ev}``— cancel
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import httpx
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Doctor, DoctorOAuthStatus
from app.db.repositories import doctors as doctors_repo

log = logging.getLogger(__name__)


GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"


# ---------------- Pydantic models exposed via tools ---------------------------


class TimeSlot(BaseModel):
    start: datetime
    end: datetime


class AvailabilityResult(BaseModel):
    doctor_id: int
    timezone: str
    requested_window: TimeSlot
    duration_minutes: int
    busy: list[TimeSlot] = Field(default_factory=list)
    free: list[TimeSlot] = Field(default_factory=list)


class CalendarEvent(BaseModel):
    event_id: str
    calendar_id: str
    summary: str | None = None
    description: str | None = None
    start: datetime
    end: datetime
    html_link: str | None = None
    attendees: list[str] = Field(default_factory=list)
    status: str | None = None


# ---------------- Token management -------------------------------------------


@dataclass
class _AccessTokenContext:
    """Mutable view of the doctor's tokens used during a single API call burst.

    Refresh happens lazily on first 401 OR when ``expires_at`` is in the past;
    the new access token is persisted before the request retries.
    """

    doctor: Doctor
    access_token: str
    refresh_token: str
    refreshed_during_call: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


async def _refresh_access_token(refresh_token: str) -> dict[str, Any]:
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError(
            "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set"
        )
    body = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(GOOGLE_TOKEN_URL, data=body)
        response.raise_for_status()
        return response.json()


async def _ensure_access_token(
    session: AsyncSession, doctor: Doctor
) -> _AccessTokenContext:
    refresh_token = doctors_repo.get_refresh_token(doctor)
    if not refresh_token:
        raise PermissionError(
            f"doctor {doctor.id} has no refresh token; re-run OAuth"
        )

    now = datetime.now(timezone.utc)
    expires_at = doctor.oauth_access_token_expires_at
    needs_refresh = (
        not doctor.oauth_access_token
        or expires_at is None
        or expires_at <= now + timedelta(seconds=60)
    )

    access_token = doctor.oauth_access_token or ""
    if needs_refresh:
        token_payload = await _refresh_access_token(refresh_token)
        access_token = token_payload["access_token"]
        new_expires = now + timedelta(seconds=int(token_payload.get("expires_in", 3600)))
        await doctors_repo.update_access_token(
            session,
            doctor.id,
            access_token=access_token,
            access_token_expires_at=new_expires,
        )

    return _AccessTokenContext(
        doctor=doctor,
        access_token=access_token,
        refresh_token=refresh_token,
    )


# ---------------- Low-level HTTP helper --------------------------------------


async def _api_call(
    session: AsyncSession,
    ctx: _AccessTokenContext,
    *,
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    url = f"{CALENDAR_API_BASE}{path}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.request(
            method,
            url,
            params=params,
            json=json_body,
            headers={"Authorization": f"Bearer {ctx.access_token}"},
        )
        # Force one refresh + retry on a 401 (token may have been revoked
        # remotely between our cached check and the actual call).
        if response.status_code == 401 and not ctx.refreshed_during_call:
            log.warning("calendar 401 for doctor %s; refreshing token + retry", ctx.doctor.id)
            token_payload = await _refresh_access_token(ctx.refresh_token)
            ctx.access_token = token_payload["access_token"]
            ctx.refreshed_during_call = True
            new_expires = datetime.now(timezone.utc) + timedelta(
                seconds=int(token_payload.get("expires_in", 3600))
            )
            await doctors_repo.update_access_token(
                session,
                ctx.doctor.id,
                access_token=ctx.access_token,
                access_token_expires_at=new_expires,
            )
            response = await client.request(
                method,
                url,
                params=params,
                json=json_body,
                headers={"Authorization": f"Bearer {ctx.access_token}"},
            )
        if response.status_code == 401:
            await doctors_repo.mark_disconnected(
                session, ctx.doctor.id, status=DoctorOAuthStatus.expired
            )
            raise PermissionError(
                f"doctor {ctx.doctor.id} OAuth refresh failed; reconnect required"
            )
        response.raise_for_status()
        if response.status_code == 204 or not response.content:
            return None
        return response.json()


# ---------------- Internal helpers -------------------------------------------


def _parse_dt(value: str) -> datetime:
    """Parse Google's date-time strings (RFC3339 with timezone)."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _to_rfc3339(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _to_event_model(payload: dict[str, Any], calendar_id: str) -> CalendarEvent:
    start = payload.get("start", {})
    end = payload.get("end", {})
    return CalendarEvent(
        event_id=payload["id"],
        calendar_id=calendar_id,
        summary=payload.get("summary"),
        description=payload.get("description"),
        start=_parse_dt(start.get("dateTime") or start.get("date")),
        end=_parse_dt(end.get("dateTime") or end.get("date")),
        html_link=payload.get("htmlLink"),
        attendees=[a.get("email") for a in payload.get("attendees", []) if a.get("email")],
        status=payload.get("status"),
    )


def _slots_between(
    window: TimeSlot, busy: list[TimeSlot], duration: timedelta
) -> list[TimeSlot]:
    """Carve free slots out of ``window`` minus ``busy`` blocks."""
    free: list[TimeSlot] = []
    cursor = window.start
    sorted_busy = sorted(busy, key=lambda b: b.start)
    for block in sorted_busy:
        if block.start > cursor:
            gap_end = min(block.start, window.end)
            while cursor + duration <= gap_end:
                free.append(TimeSlot(start=cursor, end=cursor + duration))
                cursor = cursor + duration
        cursor = max(cursor, block.end)
        if cursor >= window.end:
            break
    while cursor + duration <= window.end:
        free.append(TimeSlot(start=cursor, end=cursor + duration))
        cursor = cursor + duration
    return free


async def _resolve_doctor(session: AsyncSession, doctor_id: int) -> Doctor:
    doctor = await doctors_repo.get(session, doctor_id)
    if doctor is None:
        raise LookupError(f"doctor {doctor_id} not found")
    if doctor.oauth_status != DoctorOAuthStatus.connected:
        raise PermissionError(
            f"doctor {doctor_id} oauth_status={doctor.oauth_status.value}; reconnect required"
        )
    return doctor


# ---------------- Tool primitives --------------------------------------------


async def find_slots(
    session: AsyncSession,
    *,
    doctor_id: int,
    window: TimeSlot,
    duration_minutes: int = 30,
) -> AvailabilityResult:
    """Return free `duration_minutes`-long slots for a doctor in `window`."""
    doctor = await _resolve_doctor(session, doctor_id)
    ctx = await _ensure_access_token(session, doctor)
    body = {
        "timeMin": _to_rfc3339(window.start),
        "timeMax": _to_rfc3339(window.end),
        "items": [{"id": doctor.calendar_id}],
    }
    payload = await _api_call(session, ctx, method="POST", path="/freeBusy", json_body=body)
    assert payload is not None
    cal_block = payload.get("calendars", {}).get(doctor.calendar_id, {})
    if "errors" in cal_block:
        raise RuntimeError(f"freeBusy errors: {cal_block['errors']}")
    busy = [
        TimeSlot(start=_parse_dt(b["start"]), end=_parse_dt(b["end"]))
        for b in cal_block.get("busy", [])
    ]
    free = _slots_between(window, busy, timedelta(minutes=duration_minutes))
    return AvailabilityResult(
        doctor_id=doctor.id,
        timezone=doctor.timezone,
        requested_window=window,
        duration_minutes=duration_minutes,
        busy=busy,
        free=free,
    )


async def book_slot(
    session: AsyncSession,
    *,
    doctor_id: int,
    start: datetime,
    end: datetime,
    summary: str,
    description: str | None = None,
    patient_email: str | None = None,
    patient_phone: str | None = None,
    attendees: Iterable[str] = (),
) -> CalendarEvent:
    doctor = await _resolve_doctor(session, doctor_id)
    ctx = await _ensure_access_token(session, doctor)

    invitees: list[dict[str, Any]] = [{"email": e} for e in attendees if e]
    if patient_email:
        invitees.append({"email": patient_email, "responseStatus": "needsAction"})

    body: dict[str, Any] = {
        "summary": summary,
        "description": description or "",
        "start": {"dateTime": _to_rfc3339(start), "timeZone": doctor.timezone},
        "end": {"dateTime": _to_rfc3339(end), "timeZone": doctor.timezone},
    }
    if invitees:
        body["attendees"] = invitees
    if patient_phone:
        body["extendedProperties"] = {"private": {"medagent_patient_phone": patient_phone}}

    payload = await _api_call(
        session,
        ctx,
        method="POST",
        path=f"/calendars/{doctor.calendar_id}/events",
        params={"sendUpdates": "all"} if invitees else None,
        json_body=body,
    )
    assert payload is not None
    return _to_event_model(payload, doctor.calendar_id)


async def get_event(
    session: AsyncSession, *, doctor_id: int, event_id: str
) -> CalendarEvent:
    doctor = await _resolve_doctor(session, doctor_id)
    ctx = await _ensure_access_token(session, doctor)
    payload = await _api_call(
        session,
        ctx,
        method="GET",
        path=f"/calendars/{doctor.calendar_id}/events/{event_id}",
    )
    assert payload is not None
    return _to_event_model(payload, doctor.calendar_id)


async def reschedule_event(
    session: AsyncSession,
    *,
    doctor_id: int,
    event_id: str,
    start: datetime,
    end: datetime,
) -> CalendarEvent:
    doctor = await _resolve_doctor(session, doctor_id)
    ctx = await _ensure_access_token(session, doctor)
    body = {
        "start": {"dateTime": _to_rfc3339(start), "timeZone": doctor.timezone},
        "end": {"dateTime": _to_rfc3339(end), "timeZone": doctor.timezone},
    }
    payload = await _api_call(
        session,
        ctx,
        method="PATCH",
        path=f"/calendars/{doctor.calendar_id}/events/{event_id}",
        params={"sendUpdates": "all"},
        json_body=body,
    )
    assert payload is not None
    return _to_event_model(payload, doctor.calendar_id)


async def cancel_event(
    session: AsyncSession, *, doctor_id: int, event_id: str
) -> None:
    doctor = await _resolve_doctor(session, doctor_id)
    ctx = await _ensure_access_token(session, doctor)
    await _api_call(
        session,
        ctx,
        method="DELETE",
        path=f"/calendars/{doctor.calendar_id}/events/{event_id}",
        params={"sendUpdates": "all"},
    )


# ---------------- OAuth code-exchange helper (used by Next.js callback) ------


async def exchange_code_for_tokens(
    *, code: str, redirect_uri: str
) -> dict[str, Any]:
    """Exchange an authorization code for refresh + access tokens.

    Called by the Next.js OAuth callback (via the orchestrator's
    /doctors/{id}/oauth/callback endpoint). Returns the raw Google response
    (access_token, refresh_token, expires_in, scope, id_token).

    On a 4xx from Google the response body carries a structured error
    (``{"error": "invalid_grant", "error_description": "..."}``) — we
    surface that verbatim so the operator sees exactly what Google rejected
    instead of a generic "Client error '400'".
    """
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError(
            "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set"
        )
    body = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(GOOGLE_TOKEN_URL, data=body)
        if response.status_code >= 400:
            try:
                payload = response.json()
                err = payload.get("error", "unknown")
                detail = payload.get("error_description", "")
                raise RuntimeError(
                    f"google token exchange {response.status_code}: {err} — {detail} "
                    f"(redirect_uri sent: {redirect_uri!r})"
                )
            except ValueError:
                # Response wasn't JSON — fall back to raw text
                raise RuntimeError(
                    f"google token exchange {response.status_code}: {response.text[:500]}"
                )
        return response.json()


# ---------------- Incremental sync (Calendar → us) --------------------------


@dataclass
class CalendarChange:
    """One event change observed during an incremental sync.

    ``cancelled=True`` rows have only ``event_id`` filled — Google
    returns deleted events with ``status: "cancelled"`` and no
    other fields. Live updates carry the full event shape so the
    sweep can patch our appointment row.
    """

    event_id: str
    cancelled: bool = False
    summary: str | None = None
    start: datetime | None = None
    end: datetime | None = None


@dataclass
class IncrementalSyncResult:
    changes: list[CalendarChange] = field(default_factory=list)
    next_sync_token: str | None = None
    full_sync_required: bool = False


# How far back / forward the FIRST-time sync looks. Subsequent
# syncs are token-bounded (Google returns only changes since the
# token), so the window is irrelevant after the first call.
_INITIAL_SYNC_LOOKBACK_DAYS = 30
_INITIAL_SYNC_LOOKAHEAD_DAYS = 180


async def incremental_sync(
    session: AsyncSession,
    *,
    doctor_id: int,
) -> IncrementalSyncResult:
    """Pull events changed since the doctor's last sync token.

    First call (no stored token): performs a full initial sync
    bounded by ``[now - 30d, now + 180d]``. Subsequent calls
    pass the saved ``nextSyncToken`` and Google returns only
    events that changed since.

    Handles two failure modes:
        - 410 GONE: Google invalidated the sync token. We clear
          the saved token + return ``full_sync_required=True``;
          the caller (sweep) re-runs as a full sync next pass.
        - Pagination: ``nextPageToken`` chains pages within a
          single sync call. We follow the chain until Google
          returns ``nextSyncToken`` (only the LAST page has it).
    """
    doctor = await _resolve_doctor(session, doctor_id)
    ctx = await _ensure_access_token(session, doctor)

    base_params: dict[str, Any] = {
        "showDeleted": "true",
        "singleEvents": "true",
    }
    if doctor.gcal_sync_token:
        base_params["syncToken"] = doctor.gcal_sync_token
    else:
        # First sync — bound the window so we don't pull every
        # historical event on the doctor's calendar.
        now = datetime.now(timezone.utc)
        base_params["timeMin"] = _to_rfc3339(
            now - timedelta(days=_INITIAL_SYNC_LOOKBACK_DAYS)
        )
        base_params["timeMax"] = _to_rfc3339(
            now + timedelta(days=_INITIAL_SYNC_LOOKAHEAD_DAYS)
        )

    changes: list[CalendarChange] = []
    next_sync_token: str | None = None
    page_token: str | None = None

    while True:
        params = dict(base_params)
        if page_token:
            params["pageToken"] = page_token

        try:
            payload = await _api_call(
                session,
                ctx,
                method="GET",
                path=f"/calendars/{doctor.calendar_id}/events",
                params=params,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 410:
                # Sync token invalidated. Wipe + signal full
                # resync on next pass.
                log.warning(
                    "calendar sync token invalidated for "
                    "doctor %s; resetting",
                    doctor_id,
                )
                await doctors_repo.update_gcal_sync_state(
                    session,
                    doctor_id,
                    sync_token=None,
                    last_synced_at=None,
                )
                return IncrementalSyncResult(
                    changes=[],
                    next_sync_token=None,
                    full_sync_required=True,
                )
            raise

        if payload is None:
            break

        for item in payload.get("items", []):
            event_id = item.get("id")
            if not event_id:
                continue
            if item.get("status") == "cancelled":
                changes.append(
                    CalendarChange(
                        event_id=event_id, cancelled=True
                    )
                )
                continue
            start_obj = item.get("start") or {}
            end_obj = item.get("end") or {}
            start_str = start_obj.get("dateTime") or start_obj.get("date")
            end_str = end_obj.get("dateTime") or end_obj.get("date")
            if not start_str or not end_str:
                # Defensive — a non-cancelled event without
                # start/end is malformed. Skip.
                log.warning(
                    "incremental sync: event %s missing start/end; "
                    "skipping",
                    event_id,
                )
                continue
            changes.append(
                CalendarChange(
                    event_id=event_id,
                    cancelled=False,
                    summary=item.get("summary"),
                    start=_parse_dt(start_str),
                    end=_parse_dt(end_str),
                )
            )

        # Pagination chain. Only the LAST page carries
        # ``nextSyncToken``; intermediate pages have only
        # ``nextPageToken``.
        page_token = payload.get("nextPageToken")
        next_sync_token = payload.get("nextSyncToken") or next_sync_token
        if not page_token:
            break

    # Persist the new token + last-synced timestamp. Doing this
    # AFTER processing all pages so a mid-sync error doesn't
    # advance the cursor past unprocessed changes.
    if next_sync_token:
        await doctors_repo.update_gcal_sync_state(
            session,
            doctor_id,
            sync_token=next_sync_token,
            last_synced_at=datetime.now(timezone.utc),
        )

    return IncrementalSyncResult(
        changes=changes,
        next_sync_token=next_sync_token,
        full_sync_required=False,
    )

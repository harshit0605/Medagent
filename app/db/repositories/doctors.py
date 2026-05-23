from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Doctor, DoctorOAuthStatus
from app.security.crypto import decrypt, encrypt


async def create(
    session: AsyncSession,
    *,
    name: str,
    email: str,
    phone: str | None = None,
    timezone_name: str = "UTC",
    calendar_id: str = "primary",
) -> Doctor:
    row = Doctor(
        name=name,
        email=email,
        phone=phone,
        timezone=timezone_name,
        calendar_id=calendar_id,
        oauth_status=DoctorOAuthStatus.disconnected,
    )
    session.add(row)
    await session.flush()
    return row


async def get(session: AsyncSession, doctor_id: int) -> Doctor | None:
    return await session.get(Doctor, doctor_id)


async def get_by_email(session: AsyncSession, email: str) -> Doctor | None:
    stmt = select(Doctor).where(Doctor.email == email)
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_all(session: AsyncSession) -> list[Doctor]:
    stmt = select(Doctor).order_by(Doctor.id)
    return list((await session.execute(stmt)).scalars().all())


async def store_oauth_tokens(
    session: AsyncSession,
    doctor_id: int,
    *,
    refresh_token: str,
    access_token: str,
    access_token_expires_at: datetime,
    scopes: str,
    google_user_id: str | None,
) -> Doctor | None:
    """Persist newly-issued OAuth tokens. Refresh token is encrypted at rest.

    We ``refresh()`` after the flush so the server-computed ``updated_at``
    (``onupdate=func.now()``) is materialized inside the async context;
    otherwise a sync attribute access on the returned row would lazy-load
    and raise ``MissingGreenlet``.
    """
    row = await session.get(Doctor, doctor_id)
    if row is None:
        return None
    row.oauth_refresh_token_enc = encrypt(refresh_token)
    row.oauth_access_token = access_token
    row.oauth_access_token_expires_at = (
        access_token_expires_at.astimezone(timezone.utc)
        if access_token_expires_at.tzinfo
        else access_token_expires_at.replace(tzinfo=timezone.utc)
    )
    row.oauth_scopes = scopes
    row.google_user_id = google_user_id
    row.oauth_status = DoctorOAuthStatus.connected
    await session.flush()
    await session.refresh(row)
    return row


async def update_access_token(
    session: AsyncSession,
    doctor_id: int,
    *,
    access_token: str,
    access_token_expires_at: datetime,
) -> Doctor | None:
    """Refresh just the cached access token (no new refresh token)."""
    row = await session.get(Doctor, doctor_id)
    if row is None:
        return None
    row.oauth_access_token = access_token
    row.oauth_access_token_expires_at = (
        access_token_expires_at.astimezone(timezone.utc)
        if access_token_expires_at.tzinfo
        else access_token_expires_at.replace(tzinfo=timezone.utc)
    )
    row.oauth_status = DoctorOAuthStatus.connected
    await session.flush()
    await session.refresh(row)
    return row


async def mark_disconnected(
    session: AsyncSession,
    doctor_id: int,
    *,
    status: DoctorOAuthStatus = DoctorOAuthStatus.disconnected,
) -> Doctor | None:
    row = await session.get(Doctor, doctor_id)
    if row is None:
        return None
    row.oauth_status = status
    row.oauth_refresh_token_enc = None
    row.oauth_access_token = None
    row.oauth_access_token_expires_at = None
    await session.flush()
    await session.refresh(row)
    return row


def get_refresh_token(doctor: Doctor) -> str | None:
    """Decrypt the stored refresh token. Returns ``None`` if not set."""
    if not doctor.oauth_refresh_token_enc:
        return None
    return decrypt(doctor.oauth_refresh_token_enc)


async def update_gcal_sync_state(
    session: AsyncSession,
    doctor_id: int,
    *,
    sync_token: str | None,
    last_synced_at: datetime | None,
) -> Doctor | None:
    """Persist the latest Google Calendar incremental-sync state.

    Called by the calendar_sync_sweep after every successful pass.
    ``sync_token=None`` resets the sync (e.g. after a 410 GONE
    response from Google) — the next sweep does a full initial
    sync to seed a fresh token.
    """
    row = await session.get(Doctor, doctor_id)
    if row is None:
        return None
    row.gcal_sync_token = sync_token
    row.gcal_last_synced_at = last_synced_at
    await session.flush()
    await session.refresh(row)
    return row


async def list_connected(session: AsyncSession) -> list[Doctor]:
    """All doctors with active OAuth — the calendar_sync_sweep
    iterates this set to poll for changes."""
    stmt = select(Doctor).where(
        Doctor.oauth_status == DoctorOAuthStatus.connected
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_on_call(session: AsyncSession) -> list[Doctor]:
    """Doctors flagged for the critical-alert paging fanout.
    Used as the fallback when a patient has no
    primary-doctor history."""
    stmt = (
        select(Doctor)
        .where(Doctor.is_on_call.is_(True))
        .where(Doctor.phone.is_not(None))
    )
    return list((await session.execute(stmt)).scalars().all())


async def set_on_call(
    session: AsyncSession, doctor_id: int, *, on_call: bool
) -> Doctor | None:
    """Toggle the on-call flag. Doctors without a phone can be
    flagged but won't receive pages — surfaced as a UI warning
    at the doctors page rather than rejected here.

    We refresh after flush so server-side ``updated_at`` (set
    via ``onupdate=func.now()``) is loaded onto the row.
    Without this, callers reading ``row.updated_at`` after
    commit hit a lazy reload that races async connection
    cleanup.
    """
    row = await session.get(Doctor, doctor_id)
    if row is None:
        return None
    row.is_on_call = on_call
    await session.flush()
    await session.refresh(row)
    return row

"""Pharmacist registry persistence (MVP #5)."""

from __future__ import annotations

from sqlalchemy import asc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Pharmacist


async def get(session: AsyncSession, pharmacist_id: int) -> Pharmacist | None:
    return await session.get(Pharmacist, pharmacist_id)


async def list_all(
    session: AsyncSession, *, include_inactive: bool = False
) -> list[Pharmacist]:
    stmt = select(Pharmacist).order_by(asc(Pharmacist.full_name))
    if not include_inactive:
        stmt = stmt.where(Pharmacist.active.is_(True))
    return list((await session.execute(stmt)).scalars().all())


async def create(
    session: AsyncSession,
    *,
    full_name: str,
    phone: str | None = None,
    email: str | None = None,
    pharmacy_name: str | None = None,
) -> Pharmacist:
    row = Pharmacist(
        full_name=full_name.strip(),
        phone=(phone or "").strip() or None,
        email=(email or "").strip() or None,
        pharmacy_name=(pharmacy_name or "").strip() or None,
        active=True,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def set_active(
    session: AsyncSession, pharmacist_id: int, *, active: bool
) -> Pharmacist | None:
    row = await session.get(Pharmacist, pharmacist_id)
    if row is None:
        return None
    row.active = active
    await session.flush()
    await session.refresh(row)
    return row

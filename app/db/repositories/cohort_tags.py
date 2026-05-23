from __future__ import annotations

import re

from sqlalchemy import asc, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CohortTag, Patient, PatientCohortTag


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(label: str) -> str:
    """Make a URL-safe stable slug from a free-form label.
    "Post-MI" → "post-mi"; "Pregnancy 3T" → "pregnancy-3t"."""
    cleaned = _SLUG_RE.sub("-", label.strip().lower()).strip("-")
    return cleaned or "tag"


async def list_active(session: AsyncSession) -> list[CohortTag]:
    stmt = (
        select(CohortTag)
        .where(CohortTag.active.is_(True))
        .order_by(asc(CohortTag.label))
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_all(session: AsyncSession) -> list[CohortTag]:
    stmt = select(CohortTag).order_by(asc(CohortTag.label))
    return list((await session.execute(stmt)).scalars().all())


async def get(session: AsyncSession, tag_id: int) -> CohortTag | None:
    return await session.get(CohortTag, tag_id)


async def find_by_slug(
    session: AsyncSession, slug: str
) -> CohortTag | None:
    stmt = select(CohortTag).where(CohortTag.slug == slug).limit(1)
    return (await session.execute(stmt)).scalar_one_or_none()


async def create(
    session: AsyncSession,
    *,
    label: str,
    slug: str | None = None,
    description: str | None = None,
    created_by: str | None = None,
) -> CohortTag:
    """Caller is expected to check for slug collision first (via
    ``find_by_slug``) so the endpoint can return a clean 409 instead
    of letting IntegrityError bubble up."""
    row = CohortTag(
        slug=slug or slugify(label),
        label=label.strip(),
        description=description,
        active=True,
        created_by=created_by,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def update(
    session: AsyncSession,
    tag_id: int,
    *,
    label: str | None = None,
    description: str | None = None,
    active: bool | None = None,
) -> CohortTag | None:
    """Partial update. ``slug`` is intentionally NOT mutable — slug is
    the stable identifier; renaming label is fine but slug stays. If a
    clinician truly needs a different slug they can deactivate the old
    tag and create a new one (and reassign the patients)."""
    row = await session.get(CohortTag, tag_id)
    if row is None:
        return None
    if label is not None:
        row.label = label.strip()
    if description is not None:
        row.description = description
    if active is not None:
        row.active = active
    await session.flush()
    await session.refresh(row)
    return row


async def patient_count(
    session: AsyncSession, tag_id: int
) -> int:
    """Number of patients currently assigned to this tag. Used by the
    UI so a clinician can see "deactivating this tag will affect N
    patients" before they pull the trigger."""
    stmt = select(func.count(PatientCohortTag.id)).where(
        PatientCohortTag.cohort_tag_id == tag_id
    )
    return (await session.execute(stmt)).scalar_one()


# ---- Patient ↔ tag assignments --------------------------------------


async def list_for_patient(
    session: AsyncSession, patient_id: int
) -> list[tuple[PatientCohortTag, CohortTag]]:
    """Returns each assignment paired with the tag row so the UI can
    render labels + descriptions without a second round-trip."""
    stmt = (
        select(PatientCohortTag, CohortTag)
        .join(CohortTag, PatientCohortTag.cohort_tag_id == CohortTag.id)
        .where(PatientCohortTag.patient_id == patient_id)
        .order_by(desc(PatientCohortTag.assigned_at))
    )
    rows = (await session.execute(stmt)).all()
    return [(assignment, tag) for assignment, tag in rows]


async def patients_for_tag(
    session: AsyncSession, tag_id: int
) -> list[Patient]:
    """All patients with this tag — used by the sweep when materialising
    standing-orders for a tag-based care plan."""
    stmt = (
        select(Patient)
        .join(PatientCohortTag, PatientCohortTag.patient_id == Patient.id)
        .where(PatientCohortTag.cohort_tag_id == tag_id)
        .order_by(Patient.id)
    )
    return list((await session.execute(stmt)).scalars().all())


async def assign(
    session: AsyncSession,
    *,
    patient_id: int,
    cohort_tag_id: int,
    assigned_by: str | None = None,
) -> PatientCohortTag | None:
    """Idempotent: if the assignment already exists, returns it
    unchanged. The unique constraint protects us from duplicates even
    if two operators race."""
    stmt = (
        select(PatientCohortTag)
        .where(PatientCohortTag.patient_id == patient_id)
        .where(PatientCohortTag.cohort_tag_id == cohort_tag_id)
        .limit(1)
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        return existing
    row = PatientCohortTag(
        patient_id=patient_id,
        cohort_tag_id=cohort_tag_id,
        assigned_by=assigned_by,
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    return row


async def remove(
    session: AsyncSession,
    *,
    patient_id: int,
    cohort_tag_id: int,
) -> bool:
    """Hard-delete the assignment. Returns True if a row was deleted,
    False if no assignment existed (idempotent from the caller's POV)."""
    stmt = (
        select(PatientCohortTag)
        .where(PatientCohortTag.patient_id == patient_id)
        .where(PatientCohortTag.cohort_tag_id == cohort_tag_id)
        .limit(1)
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is None:
        return False
    await session.delete(existing)
    await session.flush()
    return True

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Patient

# Session-scoped memoization key for ``get_by_phone`` lookups. We attach to
# ``AsyncSession.info`` (a dict that lives for the session's lifetime) so the
# cache is automatically invalidated when the session closes — no manual TTL,
# no cross-session ORM-identity pitfalls.
#
# Why this matters: on every inbound WhatsApp message, ``/route`` looks up the
# patient by phone to read ``preferred_language``, and then 4–7 downstream
# handlers do the same lookup (dose_handler, refill_handler, vitals_handler,
# etc. — see callers of ``get_by_phone``). Memoizing within the request
# collapses those to a single round-trip. Reads-after-write inside the same
# session see fresh data because mutations go through the SQLAlchemy identity
# map on the cached row.
_PATIENT_BY_PHONE_CACHE_KEY = "_medagent_patient_by_phone_cache"


def _phone_cache(session: AsyncSession) -> dict[str, Patient | None]:
    cache = session.info.get(_PATIENT_BY_PHONE_CACHE_KEY)
    if cache is None:
        cache = {}
        session.info[_PATIENT_BY_PHONE_CACHE_KEY] = cache
    return cache


def _invalidate_phone(session: AsyncSession, phone: str) -> None:
    """Drop ``phone`` from the per-session cache.

    Call after writes that create / delete a patient row keyed by phone
    (``upsert_by_phone`` insert path; erasure flow). Field-level mutations
    don't need invalidation — they're applied to the same ORM row via the
    identity map and the next ``get_by_phone`` returns the cached, now-mutated
    instance.
    """
    cache = session.info.get(_PATIENT_BY_PHONE_CACHE_KEY)
    if cache is not None:
        cache.pop(phone, None)


async def get_by_phone(session: AsyncSession, phone: str) -> Patient | None:
    cache = _phone_cache(session)
    if phone in cache:
        return cache[phone]
    stmt = select(Patient).where(Patient.phone == phone)
    row = (await session.execute(stmt)).scalar_one_or_none()
    cache[phone] = row
    return row


async def get(session: AsyncSession, patient_id: int) -> Patient | None:
    return await session.get(Patient, patient_id)


async def list_all(
    session: AsyncSession, *, limit: int = 200
) -> list[Patient]:
    """List patients newest-first for the ops-console patients page."""
    stmt = (
        select(Patient).order_by(Patient.created_at.desc()).limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def search(
    session: AsyncSession, *, query: str, limit: int = 25
) -> list[Patient]:
    """Full-text-ish patient search across name, phone, external_id, and the
    medication names of their regimens. Case-insensitive substring match
    (ILIKE) — adequate for clinic-scale data and avoids a tsvector migration;
    promote to a GIN tsvector index if the patient count grows large.

    Erased patients are excluded (their PII is scrubbed; searching the
    anonymized token is pointless and could confuse operators)."""
    from app.db.models import Regimen

    q = query.strip()
    if not q:
        return []
    like = f"%{q}%"

    # Subquery: patient_ids whose regimens' medication matches.
    med_match = (
        select(Regimen.patient_id)
        .where(Regimen.medication_name.ilike(like))
        .scalar_subquery()
    )
    stmt = (
        select(Patient)
        .where(Patient.erased_at.is_(None))
        .where(
            Patient.full_name.ilike(like)
            | Patient.phone.ilike(like)
            | Patient.external_id.ilike(like)
            | Patient.id.in_(med_match)
        )
        .order_by(Patient.created_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def upsert_by_phone(
    session: AsyncSession,
    *,
    phone: str,
    default_full_name: str | None = None,
) -> Patient:
    """Return an existing Patient by phone, or create a stub one.

    Newly-created rows enter the onboarding state machine at ``pending``
    so the orchestrator's onboarding handler greets them on next message.
    Existing rows are returned untouched."""
    existing = await get_by_phone(session, phone)
    if existing is not None:
        return existing
    row = Patient(
        phone=phone,
        full_name=default_full_name or f"Patient {phone}",
        onboarding_step="pending",
    )
    session.add(row)
    await session.flush()
    await session.refresh(row)
    # The cached lookup above just stored ``None``; replace it with the new
    # row so a sibling call inside the same session sees the just-created
    # patient instead of re-querying or (worse) believing they don't exist.
    _phone_cache(session)[phone] = row
    return row


async def update_onboarding(
    session: AsyncSession,
    patient_id: int,
    *,
    step: str | None = None,
    full_name: str | None = None,
    cohort_diabetes: bool | None = None,
    cohort_cardiac: bool | None = None,
    cohort_fall_risk: bool | None = None,
    cohort_asthma: bool | None = None,
    cohort_post_op: bool | None = None,
    cohort_pregnancy: bool | None = None,
    consent_sms: bool | None = None,
) -> Patient | None:
    """Apply onboarding state transitions in a single shot. Each kwarg is
    optional; only the supplied fields are written.

    Whenever ``step`` is supplied the helper also stamps
    ``onboarding_step_at = now()`` and resets ``onboarding_retry_count``
    to 0 — every state transition (advance OR stale-reset) starts with
    a fresh retry budget and an updated last-transition timestamp."""
    row = await session.get(Patient, patient_id)
    if row is None:
        return None
    if step is not None:
        row.onboarding_step = step
        row.onboarding_step_at = datetime.now(timezone.utc)
        row.onboarding_retry_count = 0
    if full_name is not None:
        row.full_name = full_name
    if cohort_diabetes is not None:
        row.cohort_diabetes = cohort_diabetes
    if cohort_cardiac is not None:
        row.cohort_cardiac = cohort_cardiac
    if cohort_fall_risk is not None:
        row.cohort_fall_risk = cohort_fall_risk
    if cohort_asthma is not None:
        row.cohort_asthma = cohort_asthma
    if cohort_post_op is not None:
        row.cohort_post_op = cohort_post_op
    if cohort_pregnancy is not None:
        row.cohort_pregnancy = cohort_pregnancy
    if consent_sms is not None:
        row.consent_sms = consent_sms
    await session.flush()
    await session.refresh(row)
    return row


async def bump_onboarding_retry(
    session: AsyncSession, patient_id: int
) -> int | None:
    """Increment ``onboarding_retry_count`` by 1 and return the new
    value. Used by the onboarding handler when a re-prompt fires —
    crossing the escalation threshold opens an ops ticket. Returns
    None if the patient row doesn't exist."""
    row = await session.get(Patient, patient_id)
    if row is None:
        return None
    row.onboarding_retry_count = (row.onboarding_retry_count or 0) + 1
    await session.flush()
    return row.onboarding_retry_count


async def update_name(
    session: AsyncSession, patient_id: int, *, full_name: str
) -> Patient | None:
    row = await session.get(Patient, patient_id)
    if row is None:
        return None
    row.full_name = full_name
    await session.flush()
    await session.refresh(row)
    return row


async def update_preferred_language(
    session: AsyncSession, patient_id: int, *, preferred_language: str
) -> Patient | None:
    """Set the patient's preferred_language. Validation against the
    language allowlist happens at the orchestrator endpoint — the repo
    just writes whatever it's given."""
    row = await session.get(Patient, patient_id)
    if row is None:
        return None
    row.preferred_language = preferred_language
    await session.flush()
    await session.refresh(row)
    return row


async def revoke_consent(
    session: AsyncSession,
    patient_id: int,
    *,
    reason: str = "patient_stop_keyword",
    when: datetime | None = None,
) -> Patient | None:
    """Flip ``consent_sms`` to False and stamp the audit columns.

    Idempotent — if consent was already revoked, the timestamp + reason
    stay at their original values. That preserves the original opt-out
    moment in the timeline rather than overwriting it on a duplicate
    STOP message.
    """
    row = await session.get(Patient, patient_id)
    if row is None:
        return None
    if row.consent_sms or row.consent_revoked_at is None:
        row.consent_sms = False
        row.consent_revoked_at = when or datetime.now(timezone.utc)
        row.consent_revoked_reason = reason
    await session.flush()
    await session.refresh(row)
    return row


async def restore_consent(
    session: AsyncSession, patient_id: int
) -> Patient | None:
    """Flip ``consent_sms`` to True and clear the revoke audit columns.

    Used by the START / opt-in path. Doesn't restore voice/email
    consent — those have separate channels and need their own opt-in.
    """
    row = await session.get(Patient, patient_id)
    if row is None:
        return None
    row.consent_sms = True
    row.consent_revoked_at = None
    row.consent_revoked_reason = None
    await session.flush()
    await session.refresh(row)
    return row


async def pause_bot(
    session: AsyncSession,
    patient_id: int,
    *,
    actor: str,
    reason: str,
    when: datetime | None = None,
) -> Patient | None:
    """Stamp the ops-initiated bot-pause audit columns. Idempotent —
    re-pausing an already-paused patient leaves the original
    ``bot_paused_at`` timestamp + actor untouched but UPDATES the
    reason (a follow-up "still investigating" reason supersedes
    the original "complaint received" without losing the moment
    the pause started).

    Distinct from ``revoke_consent`` — this does NOT touch
    ``consent_sms`` and does NOT trigger any patient-facing ack.
    The dispatcher reads ``bot_paused_at`` on every send and
    short-circuits if it's set."""
    row = await session.get(Patient, patient_id)
    if row is None:
        return None
    if row.bot_paused_at is None:
        row.bot_paused_at = when or datetime.now(timezone.utc)
        row.bot_paused_by = actor
    row.bot_paused_reason = reason
    await session.flush()
    await session.refresh(row)
    return row


async def unpause_bot(
    session: AsyncSession,
    patient_id: int,
) -> Patient | None:
    """Clear the bot-pause audit columns. Outbound resumes on the
    next dispatcher tick. Does NOT send a patient-facing notification
    — pause / unpause are invisible to the patient by design (they
    didn't see the pause, they don't see the unpause)."""
    row = await session.get(Patient, patient_id)
    if row is None:
        return None
    row.bot_paused_at = None
    row.bot_paused_reason = None
    row.bot_paused_by = None
    await session.flush()
    await session.refresh(row)
    return row

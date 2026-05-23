"""End-to-end integration tests for the caregiver inbound consent
handler against a real DB. Verifies:

- A caregiver in ``pending`` status flips to ``confirmed`` on YES.
- Same row flips to ``declined`` on NO.
- Marker-form ``[caregiver-action] confirm caregiver_id=N`` works.
- Already-confirmed rows stay idempotent on a second YES.
- A YES from a phone with no pending caregiver returns None
  (orchestrator falls through to the LLM path).
"""

from __future__ import annotations

import os
import uuid

import pytest

from app.db.models import Patient
from app.db.repositories import caregivers as caregivers_repo
from app.db.session import get_sessionmaker
from services.orchestrator import caregiver_handler

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping caregiver handler integration tests",
)


async def _seed_pending_caregiver(*, phone: str) -> tuple[int, int]:
    """Create a fresh patient + pending caregiver. Returns
    (patient_id, caregiver_id)."""
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(
            full_name=f"CG Inbound Test {suffix}",
            phone=f"cg-inbound-patient-{suffix}",
        )
        db.add(p)
        await db.flush()
        cg = await caregivers_repo.create(
            db,
            patient_id=p.id,
            full_name=f"Care Contact {suffix}",
            phone=phone,
            relationship_to_patient="spouse",
        )
        await db.commit()
        return p.id, cg.id


async def test_yes_confirms_pending_caregiver_e2e():
    suffix = uuid.uuid4().hex[:8]
    phone = f"cg-yes-{suffix}"
    _, cg_id = await _seed_pending_caregiver(phone=phone)

    delta = await caregiver_handler.handle_caregiver_action(
        sender_phone=phone, new_user_text="YES"
    )
    assert delta is not None
    assert delta["audit_reasons"] == ["caregiver_action_confirmed"]

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        cg = await caregivers_repo.get(db, cg_id)
    assert cg.consent_status == caregivers_repo.CONSENT_CONFIRMED
    assert cg.consent_confirmed_by == "caregiver_yes_reply"
    assert cg.consent_confirmed_at is not None


async def test_no_declines_pending_caregiver_e2e():
    suffix = uuid.uuid4().hex[:8]
    phone = f"cg-no-{suffix}"
    _, cg_id = await _seed_pending_caregiver(phone=phone)

    delta = await caregiver_handler.handle_caregiver_action(
        sender_phone=phone, new_user_text="No"
    )
    assert delta is not None
    assert delta["audit_reasons"] == ["caregiver_action_declined"]

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        cg = await caregivers_repo.get(db, cg_id)
    assert cg.consent_status == caregivers_repo.CONSENT_DECLINED


async def test_marker_form_confirms_with_id_hint_e2e():
    suffix = uuid.uuid4().hex[:8]
    phone = f"cg-marker-{suffix}"
    _, cg_id = await _seed_pending_caregiver(phone=phone)

    delta = await caregiver_handler.handle_caregiver_action(
        sender_phone=phone,
        new_user_text=f"[caregiver-action] confirm caregiver_id={cg_id}",
    )
    assert delta is not None
    assert delta["audit_reasons"] == ["caregiver_action_confirmed"]

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        cg = await caregivers_repo.get(db, cg_id)
    assert cg.consent_status == caregivers_repo.CONSENT_CONFIRMED


async def test_yes_after_confirm_is_idempotent_e2e():
    """Re-tap of YES on an already-confirmed caregiver returns the
    "already confirmed" message and DOESN'T mutate the row a second
    time (timestamp + by-tag stay locked)."""
    suffix = uuid.uuid4().hex[:8]
    phone = f"cg-idem-{suffix}"
    _, cg_id = await _seed_pending_caregiver(phone=phone)

    # First YES.
    await caregiver_handler.handle_caregiver_action(
        sender_phone=phone, new_user_text="YES"
    )
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        cg_after_first = await caregivers_repo.get(db, cg_id)
    first_at = cg_after_first.consent_confirmed_at

    # Second YES.
    delta2 = await caregiver_handler.handle_caregiver_action(
        sender_phone=phone, new_user_text="YES"
    )
    # Marker form to land in the by-id branch (plain YES with no
    # *pending* row falls through with None).
    delta3 = await caregiver_handler.handle_caregiver_action(
        sender_phone=phone,
        new_user_text=f"[caregiver-action] confirm caregiver_id={cg_id}",
    )
    # The plain "YES" with no pending row → None (fallthrough).
    assert delta2 is None
    # The marker form → idempotent already-confirmed message.
    assert delta3 is not None
    assert delta3["audit_reasons"] == ["caregiver_action_already_confirmed"]

    async with SessionLocal() as db:
        cg_after_second = await caregivers_repo.get(db, cg_id)
    # Timestamp didn't move forward — original confirmation preserved.
    assert cg_after_second.consent_confirmed_at == first_at


async def test_yes_with_no_pending_returns_none():
    """Phone with no pending caregiver row → handler returns None so
    the orchestrator falls through to the regular LLM path. Must NOT
    accidentally confirm someone else's row."""
    out = await caregiver_handler.handle_caregiver_action(
        sender_phone=f"cg-stray-{uuid.uuid4().hex[:6]}",
        new_user_text="YES",
    )
    assert out is None

"""Integration smoke for the caregiver dose-reminder fan-out repo + handler.

Verifies the new ``list_active_dose_recipients`` SQL filter against real
Postgres + the dose_handler's caregiver-attribution path resolves a real
caregiver row by phone. The dispatcher's fan-out plumbing is covered by
unit tests (mocked); this test is the end-to-end SQL/ORM check.

Skipped when DATABASE_URL is unset.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import AdherenceEvent, AdherenceStatus, Patient, Regimen
from app.db.repositories import caregivers as caregivers_repo
from app.db.session import get_sessionmaker

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping integration tests",
)


async def _seed_patient_with_caregiver(
    *, cg_notify_dose: bool, cg_consent_status: str = "confirmed"
) -> tuple[int, str, str]:
    """Returns (patient_id, patient_phone, caregiver_phone)."""
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(
            full_name=f"Asha {suffix}",
            phone=f"cgfanout-pt-{suffix}",
            consent_sms=True,
        )
        db.add(p)
        await db.flush()
        cg = await caregivers_repo.create(
            db,
            patient_id=p.id,
            full_name=f"Family {suffix}",
            phone=f"cgfanout-cg-{suffix}",
        )
        cg.notify_on_dose_reminder = cg_notify_dose
        cg.consent_status = cg_consent_status
        if cg_consent_status == "confirmed":
            cg.consent_confirmed_at = datetime.now(timezone.utc)
            cg.consent_confirmed_by = "ops"
        await db.flush()
        await db.commit()
        return p.id, p.phone, cg.phone


async def test_list_active_dose_recipients_filters_correctly():
    pid_opted_in, _, cg_phone_in = await _seed_patient_with_caregiver(
        cg_notify_dose=True
    )
    pid_opted_out, _, _ = await _seed_patient_with_caregiver(
        cg_notify_dose=False
    )
    pid_pending, _, _ = await _seed_patient_with_caregiver(
        cg_notify_dose=True, cg_consent_status="pending"
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        in_list = await caregivers_repo.list_active_dose_recipients(
            db, pid_opted_in
        )
        out_list = await caregivers_repo.list_active_dose_recipients(
            db, pid_opted_out
        )
        pending_list = await caregivers_repo.list_active_dose_recipients(
            db, pid_pending
        )

    assert [cg.phone for cg in in_list] == [cg_phone_in]
    assert out_list == [], "notify_on_dose_reminder=False excludes the row"
    assert pending_list == [], "consent_status=pending excludes the row"


async def test_find_active_confirmed_by_phone_scopes_to_patient():
    """A caregiver phone registered for patient A should NOT match a lookup
    scoped to patient B (defense against confused-action attribution)."""
    pid_a, _, cg_phone = await _seed_patient_with_caregiver(
        cg_notify_dose=True
    )
    pid_b, _, _ = await _seed_patient_with_caregiver(cg_notify_dose=True)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        hit = await caregivers_repo.find_active_confirmed_by_phone(
            db, phone=cg_phone, patient_id=pid_a
        )
        miss = await caregivers_repo.find_active_confirmed_by_phone(
            db, phone=cg_phone, patient_id=pid_b
        )

    assert hit is not None
    assert hit.phone == cg_phone
    assert miss is None


async def test_dose_handler_records_caregiver_attribution():
    """End-to-end: caregiver's phone TAKEN reply attributes the action via
    confirmation_metadata."""
    from services.orchestrator import dose_handler

    pid, _patient_phone, cg_phone = await _seed_patient_with_caregiver(
        cg_notify_dose=True
    )
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        regimen = Regimen(
            patient_id=pid,
            medication_name="Metformin",
            dose="500mg",
            schedule={"kind": "fixed", "times": ["09:00"]},
        )
        db.add(regimen)
        await db.flush()
        adh = AdherenceEvent(
            patient_id=pid,
            regimen_id=regimen.id,
            scheduled_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            status=AdherenceStatus.scheduled,
        )
        db.add(adh)
        await db.flush()
        await db.commit()
        adh_id = adh.id

    delta = await dose_handler.handle_dose_action(
        patient_phone=cg_phone,
        new_user_text=f"[dose-action] taken adherence_event_id={adh_id}",
    )
    assert delta is not None
    assert "Logged" in delta["response_body"]
    assert "dose_action_taken" in delta["audit_reasons"]

    # Confirmation metadata carries the caregiver attribution.
    async with SessionLocal() as db:
        row = await db.get(AdherenceEvent, adh_id)
        assert row is not None
        assert row.status == AdherenceStatus.taken
        meta = row.confirmation_metadata or {}
        assert "acted_by_caregiver_id" in meta
        assert meta["acted_by_phone"] == cg_phone

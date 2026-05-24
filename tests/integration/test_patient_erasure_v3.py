"""Right-of-erasure coverage for the newer feature tables.

P3–P6 + the multi-patient-household work added tables that snapshot patient
PII: pregnancies (notes), orders (notes / deep-link embedding the phone /
receipt URL / partner order id), post_op_episodes (notes), asthma_trigger_logs
(the patient's own words), and households (primary_caregiver_phone). The new
``cohort_asthma`` / ``cohort_post_op`` / ``cohort_pregnancy`` flags and the
``household_id`` membership link also weren't being cleared.

These tests seed each surface with PII, run the erasure flow, and confirm the
PII is scrubbed while anonymized clinical/structural data is retained.

DATABASE_URL required.
"""

from __future__ import annotations

import os
import uuid
from datetime import date

import pytest
from sqlalchemy import select

from app.db.models import (
    AsthmaTriggerLog,
    Household,
    Order,
    Patient,
    PostOpEpisode,
    Pregnancy,
)
from app.db.session import get_sessionmaker
from services.orchestrator import patient_erasure

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping V3 erasure tests",
)


async def _seed_patient_with_v3_pii() -> tuple[int, str, int]:
    """Create a patient who is the primary caregiver of a household and has a
    row in every new PII-bearing table."""
    suffix = uuid.uuid4().hex[:8]
    phone = f"v3erase-{suffix}"
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        household = Household(
            name=f"Test Family {suffix}",
            primary_caregiver_phone=phone,
            notes="household notes",
        )
        db.add(household)
        await db.flush()

        p = Patient(
            full_name=f"V3 Erase {suffix}",
            phone=phone,
            consent_sms=True,
            cohort_asthma=True,
            cohort_post_op=True,
            cohort_pregnancy=True,
            household_id=household.id,
        )
        db.add(p)
        await db.flush()

        db.add(
            Pregnancy(
                patient_id=p.id,
                lmp_date=date(2026, 1, 1),
                edd=date(2026, 10, 8),
                status="active",
                notes="patient said baby due in spring",
            )
        )
        db.add(
            Order(
                patient_id=p.id,
                medication_name="Atorvastatin",
                dose="10 mg",
                partner="acme_rx",
                status="pending",
                notes="leave at the front door",
                substitution_note="patient prefers brand",
                partner_deeplink=f"https://acme.example/order?phone={phone}",
                receipt_url="https://acme.example/receipt/123",
                partner_order_id="EXT-123",
            )
        )
        db.add(
            PostOpEpisode(
                patient_id=p.id,
                procedure_name="Appendectomy",
                surgery_date=date(2026, 5, 1),
                status="active",
                notes="patient is anxious about the wound",
            )
        )
        db.add(
            AsthmaTriggerLog(
                patient_id=p.id,
                trigger_text="dust while cleaning the house",
                source="patient_self_report",
            )
        )
        await db.commit()
        return p.id, phone, household.id


async def _erase(pid: int) -> None:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await patient_erasure.erase_patient_data(
            db, patient_id=pid, actor="ops", reason="test"
        )
        await db.commit()


async def test_erasure_scrubs_pregnancy_pii():
    pid, _phone, _hh = await _seed_patient_with_v3_pii()
    await _erase(pid)
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows = list(
            (
                await db.execute(
                    select(Pregnancy).where(Pregnancy.patient_id == pid)
                )
            ).scalars().all()
        )
    assert rows
    for row in rows:
        assert row.notes is None
        assert row.ended_reason is None
        # Clinical dates retained as now-anonymized data.
        assert row.lmp_date == date(2026, 1, 1)


async def test_erasure_scrubs_order_pii():
    pid, phone, _hh = await _seed_patient_with_v3_pii()
    await _erase(pid)
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows = list(
            (
                await db.execute(select(Order).where(Order.patient_id == pid))
            ).scalars().all()
        )
    assert rows
    for row in rows:
        assert row.notes is None
        assert row.substitution_note is None
        assert row.partner_deeplink is None  # embedded the phone
        assert row.receipt_url is None
        assert row.partner_order_id is None
        # Clinical snapshot retained (like regimens).
        assert row.medication_name == "Atorvastatin"


async def test_erasure_scrubs_post_op_pii():
    pid, _phone, _hh = await _seed_patient_with_v3_pii()
    await _erase(pid)
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows = list(
            (
                await db.execute(
                    select(PostOpEpisode).where(
                        PostOpEpisode.patient_id == pid
                    )
                )
            ).scalars().all()
        )
    assert rows
    for row in rows:
        assert row.notes is None
        assert row.ended_reason is None
        assert row.procedure_name == "Appendectomy"


async def test_erasure_overwrites_asthma_trigger_text():
    pid, _phone, _hh = await _seed_patient_with_v3_pii()
    await _erase(pid)
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows = list(
            (
                await db.execute(
                    select(AsthmaTriggerLog).where(
                        AsthmaTriggerLog.patient_id == pid
                    )
                )
            ).scalars().all()
        )
    assert rows
    for row in rows:
        assert row.trigger_text == "[erased]"


async def test_erasure_rotates_household_phone_and_clears_membership():
    pid, original_phone, hh_id = await _seed_patient_with_v3_pii()
    await _erase(pid)
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        household = await db.get(Household, hh_id)
        patient = await db.get(Patient, pid)
    # Caregiver phone rotated off the original number.
    assert household.primary_caregiver_phone != original_phone
    assert household.primary_caregiver_phone.startswith("erased:")
    # Membership dropped (household itself preserved for other members).
    assert patient.household_id is None


async def test_erasure_clears_new_cohort_flags():
    pid, _phone, _hh = await _seed_patient_with_v3_pii()
    await _erase(pid)
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        patient = await db.get(Patient, pid)
    assert patient.cohort_asthma is False
    assert patient.cohort_post_op is False
    assert patient.cohort_pregnancy is False

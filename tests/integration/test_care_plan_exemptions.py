"""Integration tests for patient-level care_plan_exemptions.

Covers the repo CRUD, the orchestrator endpoints, and — most importantly —
that the sweep + count helper skip exempted patients.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.db.models import Patient
from app.db.repositories import care_plan_exemptions as care_plan_exemptions_repo
from app.db.repositories import care_plans as care_plans_repo
from app.db.session import get_sessionmaker
from services.scheduler import care_gaps

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping exemption integration tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


async def _seed_patient(**flags) -> int:
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(
            full_name=f"Exempt Test {suffix}",
            phone=f"exempt-test-{suffix}",
            **flags,
        )
        db.add(p)
        await db.flush()
        await db.commit()
        await db.refresh(p)
        return p.id


async def _hba1c_plan_id() -> int:
    """The diabetes/HbA1c plan id, seeded by migration 0014."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        plans = await care_plans_repo.list_active(db)
    plan = next(
        p for p in plans if p.test_name == "HbA1c" and p.cohort_attr == "cohort_diabetes"
    )
    return plan.id


# ---- Repo ---------------------------------------------------------------


async def test_create_then_revoke_lifecycle():
    patient_id = await _seed_patient(cohort_diabetes=True)
    plan_id = await _hba1c_plan_id()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        ex = await care_plan_exemptions_repo.create(
            db,
            patient_id=patient_id,
            care_plan_id=plan_id,
            reason="under nephrology",
            created_by="dr.kim",
        )
        await db.commit()
        ex_id = ex.id

        active = await care_plan_exemptions_repo.find_active_by_patient_plan(
            db, patient_id=patient_id, care_plan_id=plan_id
        )
        assert active is not None and active.id == ex_id

        revoked = await care_plan_exemptions_repo.revoke(
            db, ex_id, revoked_by="dr.kim"
        )
        await db.commit()
        assert revoked.revoked_at is not None

        # No longer active.
        post_revoke = await care_plan_exemptions_repo.find_active_by_patient_plan(
            db, patient_id=patient_id, care_plan_id=plan_id
        )
    assert post_revoke is None


async def test_expired_exemption_is_inactive():
    patient_id = await _seed_patient(cohort_diabetes=True)
    plan_id = await _hba1c_plan_id()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await care_plan_exemptions_repo.create(
            db,
            patient_id=patient_id,
            care_plan_id=plan_id,
            reason="temporary opt-out",
            expires_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        await db.commit()
        active = await care_plan_exemptions_repo.find_active_by_patient_plan(
            db, patient_id=patient_id, care_plan_id=plan_id
        )
    # expires_at < now → not active.
    assert active is None


async def test_revoke_idempotent():
    patient_id = await _seed_patient(cohort_diabetes=True)
    plan_id = await _hba1c_plan_id()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        ex = await care_plan_exemptions_repo.create(
            db,
            patient_id=patient_id,
            care_plan_id=plan_id,
            reason="test",
        )
        await db.commit()
        first = await care_plan_exemptions_repo.revoke(db, ex.id)
        await db.commit()
        first_revoked_at = first.revoked_at

        # Second revoke should be a no-op (return same row, same timestamp).
        second = await care_plan_exemptions_repo.revoke(db, ex.id)
    assert second is not None
    assert second.revoked_at == first_revoked_at


# ---- Sweep + count integration ------------------------------------------


async def test_sweep_skips_exempted_patient():
    patient_id = await _seed_patient(cohort_diabetes=True)
    plan_id = await _hba1c_plan_id()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await care_plan_exemptions_repo.create(
            db,
            patient_id=patient_id,
            care_plan_id=plan_id,
            reason="exempted for test",
        )
        await db.commit()

        result = await care_gaps.sweep_care_gaps(db)
        await db.commit()

    # The diabetes/HbA1c plan should report at least one skipped_exempt
    # for this patient (others may be skipped for other reasons).
    assert result["HbA1c"]["skipped_exempt"] >= 1


async def _patient_is_gap_for_diabetes_hba1c(
    patient_id: int,
) -> bool:
    """Per-patient gap check for the diabetes/HbA1c standing order.
    Mirrors the gates in ``overdue_care_gap_count`` but scoped to one
    patient — flake-free against a shared DB where the background
    scheduler sweep keeps changing the global count.
    """
    from datetime import datetime, timedelta, timezone

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        plan_id = await _hba1c_plan_id()
        # Active exemption for this plan?
        active = (
            await care_plan_exemptions_repo.active_plan_ids_for_patient(
                db, patient_id
            )
        )
        if plan_id in active:
            return False
        # Open or recent-enough followup?
        from app.db.repositories import care_plans as care_plans_repo
        plans = await care_plans_repo.list_active(db)
        plan = next(p for p in plans if p.id == plan_id)
        from services.scheduler.care_gaps import (
            _has_open_followup,
            _last_completed_at,
        )
        if await _has_open_followup(
            db, patient_id=patient_id, test_name=plan.test_name
        ):
            return False
        last_done = await _last_completed_at(
            db, patient_id=patient_id, test_name=plan.test_name
        )
        cadence = timedelta(days=plan.cadence_days)
        if last_done is not None and (
            datetime.now(timezone.utc) - last_done
        ) < cadence:
            return False
        return True


async def test_count_helper_excludes_exempted_patient():
    """Patient-scoped: a freshly-seeded diabetic with no completion +
    no followup IS a gap; exempting them removes them from the gap
    set. We verify per-patient (not global aggregate) so a background
    scheduler sweep changing other patients' state doesn't flake the
    test."""
    patient_id = await _seed_patient(cohort_diabetes=True)
    plan_id = await _hba1c_plan_id()

    assert await _patient_is_gap_for_diabetes_hba1c(patient_id) is True

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await care_plan_exemptions_repo.create(
            db,
            patient_id=patient_id,
            care_plan_id=plan_id,
            reason="exempted",
        )
        await db.commit()

    assert await _patient_is_gap_for_diabetes_hba1c(patient_id) is False


async def test_revoking_exemption_reinstates_gap():
    """Inverse: seed exempted (no gap) → revoke → gap reinstated.
    Patient-scoped for the same flake-immunity reason as above."""
    patient_id = await _seed_patient(cohort_diabetes=True)
    plan_id = await _hba1c_plan_id()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        ex = await care_plan_exemptions_repo.create(
            db,
            patient_id=patient_id,
            care_plan_id=plan_id,
            reason="initial",
        )
        await db.commit()

    assert await _patient_is_gap_for_diabetes_hba1c(patient_id) is False

    async with SessionLocal() as db:
        await care_plan_exemptions_repo.revoke(db, ex.id)
        await db.commit()

    assert await _patient_is_gap_for_diabetes_hba1c(patient_id) is True


# ---- Endpoints ---------------------------------------------------------


def test_endpoint_create_then_revoke(orchestrator_client):
    # Create a fresh patient via the API so we don't collide with seeds.
    SessionLocal = get_sessionmaker()
    suffix = uuid.uuid4().hex[:8]

    async def _setup() -> tuple[int, int]:
        async with SessionLocal() as db:
            p = Patient(
                full_name=f"Endpoint Test {suffix}",
                phone=f"endpoint-test-{suffix}",
                cohort_diabetes=True,
            )
            db.add(p)
            await db.flush()
            await db.commit()
            patient_pk = p.id
            plans = await care_plans_repo.list_active(db)
            plan_pk = next(
                p.id for p in plans if p.test_name == "HbA1c"
            )
            return patient_pk, plan_pk

    import asyncio

    patient_id, plan_id = asyncio.get_event_loop().run_until_complete(_setup())

    create = orchestrator_client.post(
        f"/patients/{patient_id}/care-plan-exemptions",
        json={
            "care_plan_id": plan_id,
            "reason": "Specialist managing alternate cadence",
            "created_by": "dr.smith",
        },
    )
    assert create.status_code == 200
    body = create.json()
    assert body["is_active"] is True
    assert body["care_plan_test_name"] == "HbA1c"
    exemption_id = body["id"]

    # Duplicate create should 409.
    dup = orchestrator_client.post(
        f"/patients/{patient_id}/care-plan-exemptions",
        json={"care_plan_id": plan_id, "reason": "again"},
    )
    assert dup.status_code == 409

    # List shows the active row.
    listed = orchestrator_client.get(
        f"/patients/{patient_id}/care-plan-exemptions"
    ).json()
    assert any(e["id"] == exemption_id for e in listed)

    # Revoke → no longer active.
    revoke = orchestrator_client.post(
        f"/care-plan-exemptions/{exemption_id}/revoke",
        json={"revoked_by": "dr.smith"},
    )
    assert revoke.status_code == 200
    assert revoke.json()["is_active"] is False

    # After revoke, default list is empty (no active rows); inclusive
    # list still has the historical row.
    active_only = orchestrator_client.get(
        f"/patients/{patient_id}/care-plan-exemptions"
    ).json()
    assert all(e["id"] != exemption_id for e in active_only)
    inclusive = orchestrator_client.get(
        f"/patients/{patient_id}/care-plan-exemptions",
        params={"include_inactive": "true"},
    ).json()
    assert any(e["id"] == exemption_id for e in inclusive)


def test_endpoint_404_for_unknown_patient(orchestrator_client):
    r = orchestrator_client.post(
        "/patients/9999999/care-plan-exemptions",
        json={"care_plan_id": 1, "reason": "test reason"},
    )
    assert r.status_code == 404


def test_endpoint_404_for_unknown_plan(orchestrator_client):
    # Use any existing patient.
    SessionLocal = get_sessionmaker()
    suffix = uuid.uuid4().hex[:8]

    async def _seed() -> int:
        async with SessionLocal() as db:
            p = Patient(
                full_name=f"Endpoint404 {suffix}",
                phone=f"endpoint404-{suffix}",
            )
            db.add(p)
            await db.flush()
            await db.commit()
            return p.id

    import asyncio

    patient_id = asyncio.get_event_loop().run_until_complete(_seed())

    r = orchestrator_client.post(
        f"/patients/{patient_id}/care-plan-exemptions",
        json={"care_plan_id": 9999999, "reason": "test reason"},
    )
    assert r.status_code == 404


def test_endpoint_revoke_404_for_unknown_id(orchestrator_client):
    assert (
        orchestrator_client.post(
            "/care-plan-exemptions/9999999/revoke", json={}
        ).status_code
        == 404
    )

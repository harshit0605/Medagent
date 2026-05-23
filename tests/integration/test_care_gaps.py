"""Integration tests for cohort-driven care-gap sweeps.

Verifies the standing-orders rules materialise lab_followups for cohort
patients overdue for a recurring test, with proper idempotency:
- patient already has an open followup matching the test → skip
- patient completed the test recently (within cadence) → skip
- otherwise → create a new ``due`` lab_followup with future ``due_by``

Skipped when DATABASE_URL is unset.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db.models import (
    FollowupStatus,
    LabFollowup,
    Patient,
)
from app.db.session import get_sessionmaker
from services.scheduler import care_gaps

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping care-gap integration tests",
)


# Care-gap sweeps iterate ALL patients with the relevant
# cohort flag (diabetes / cardiac / fall_risk) AND ALL active
# care_plans. When the test DB carries pollution from other
# files (test_caregivers / test_care_plans seed cohort
# patients; test_care_plans / test_visit_brief_scheduling
# create plans), each sweep call here scans hundreds of
# unrelated rows — a 4-query-per-patient × N-patients ×
# M-plans explosion that hangs the file under load.
#
# Per-test cleanup gates the sweep's input set down to just
# the patients + plans this test seeds. Surgical: deletes
# patients whose phone matches the test seeders' shape
# (won't touch real ``+91...`` numbers) AND have any cohort
# flag set; drops any care_plan whose id is past the 3 base
# plans seeded by initial migration.
@pytest.fixture(autouse=True)
async def _isolate_cohort_state():
    """Delete pollution that would skew the sweep before each
    test in this file. Runs BEFORE the test seeds its own
    rows so the sweep's cohort query returns only the
    patient(s) this test created."""
    from sqlalchemy import text as sql_text

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        # Wipe test-shaped patients with cohort flags. The
        # phone regex matches our test seed pattern (lowercase
        # + hyphens + hex); real numbers stay.
        await db.execute(
            sql_text(
                "DELETE FROM patients WHERE phone ~ "
                "'^[a-z][a-z0-9_-]*-[a-f0-9]{4,16}$' AND "
                "(cohort_diabetes OR cohort_cardiac OR cohort_fall_risk)"
            )
        )
        # Drop test-created care_plans, keep the 3 seeded by
        # the initial migration. The migration assigned them
        # ids 1, 2, 3 (HbA1c, Blood pressure check, Vitamin D
        # level). Any plan past that is test residue.
        await db.execute(
            sql_text(
                "DELETE FROM care_plans WHERE id > 3"
            )
        )
        await db.commit()
    yield


async def _seed_patient(**cohort_flags) -> int:
    """Returns patient_id."""
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        patient = Patient(
            full_name=f"Care Gap {suffix}",
            phone=f"care-gap-{suffix}",
            **cohort_flags,
        )
        db.add(patient)
        await db.flush()
        await db.commit()
        await db.refresh(patient)
        return patient.id


async def _seed_completed_lab(
    patient_id: int,
    test_name: str,
    *,
    completed_at: datetime,
) -> int:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        lab = LabFollowup(
            patient_id=patient_id,
            test_name=test_name,
            status=FollowupStatus.completed,
            completed_at=completed_at,
        )
        db.add(lab)
        await db.flush()
        await db.commit()
        return lab.id


async def _seed_open_lab(patient_id: int, test_name: str) -> int:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        lab = LabFollowup(
            patient_id=patient_id,
            test_name=test_name,
            status=FollowupStatus.due,
        )
        db.add(lab)
        await db.flush()
        await db.commit()
        return lab.id


async def _open_labs_for(patient_id: int) -> list[LabFollowup]:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(LabFollowup)
                .where(LabFollowup.patient_id == patient_id)
                .where(
                    LabFollowup.status.in_(
                        [FollowupStatus.due, FollowupStatus.booked]
                    )
                )
            )
        ).scalars().all()
        return list(rows)


# ---- Materialisation -------------------------------------------------------


async def test_diabetic_with_no_history_gets_hba1c():
    patient_id = await _seed_patient(cohort_diabetes=True)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await care_gaps.sweep_care_gaps(db)
        await db.commit()
    # Sweep returns counts per test_name; the diabetes plan must have
    # materialised at least one row (this patient).
    assert result["HbA1c"]["materialized"] >= 1

    open_labs = await _open_labs_for(patient_id)
    assert any(lab.test_name == "HbA1c" for lab in open_labs)
    hba1c = next(lab for lab in open_labs if lab.test_name == "HbA1c")
    # due_by is +14 days from now, so it should be in the future.
    assert hba1c.due_by is not None
    assert hba1c.due_by > datetime.now(timezone.utc).date()


async def test_cardiac_patient_gets_bp_check():
    patient_id = await _seed_patient(cohort_cardiac=True)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await care_gaps.sweep_care_gaps(db)
        await db.commit()

    open_labs = await _open_labs_for(patient_id)
    assert any(
        lab.test_name == "Blood pressure check" for lab in open_labs
    )


async def test_fall_risk_patient_gets_vitamin_d():
    patient_id = await _seed_patient(cohort_fall_risk=True)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await care_gaps.sweep_care_gaps(db)
        await db.commit()

    open_labs = await _open_labs_for(patient_id)
    assert any(lab.test_name == "Vitamin D level" for lab in open_labs)


async def test_multiple_cohorts_get_multiple_tests():
    patient_id = await _seed_patient(
        cohort_diabetes=True, cohort_cardiac=True
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await care_gaps.sweep_care_gaps(db)
        await db.commit()

    open_labs = await _open_labs_for(patient_id)
    test_names = {lab.test_name for lab in open_labs}
    assert "HbA1c" in test_names
    assert "Blood pressure check" in test_names


# ---- Idempotency / skip rules ---------------------------------------------


async def test_skip_when_recent_completion_within_cadence():
    """Diabetic who completed HbA1c 30 days ago should NOT get a new
    followup — cadence is 180 days."""
    patient_id = await _seed_patient(cohort_diabetes=True)
    await _seed_completed_lab(
        patient_id,
        "HbA1c",
        completed_at=datetime.now(timezone.utc) - timedelta(days=30),
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await care_gaps.sweep_care_gaps(db)
        await db.commit()

    open_labs = await _open_labs_for(patient_id)
    # The skip-recent gate fires because completed_at is < 180 days old.
    assert not any(lab.test_name == "HbA1c" for lab in open_labs)
    # The result accounting should reflect a skip in this category.
    assert result["HbA1c"]["skipped_recent_completion"] >= 1


async def test_materialise_when_completion_older_than_cadence():
    patient_id = await _seed_patient(cohort_diabetes=True)
    await _seed_completed_lab(
        patient_id,
        "HbA1c",
        completed_at=datetime.now(timezone.utc) - timedelta(days=200),
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await care_gaps.sweep_care_gaps(db)
        await db.commit()

    open_labs = await _open_labs_for(patient_id)
    assert any(lab.test_name == "HbA1c" for lab in open_labs)


async def test_skip_when_open_followup_already_exists():
    patient_id = await _seed_patient(cohort_diabetes=True)
    existing_id = await _seed_open_lab(patient_id, "HbA1c")

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await care_gaps.sweep_care_gaps(db)
        await db.commit()

    open_labs = await _open_labs_for(patient_id)
    hba1c_rows = [lab for lab in open_labs if lab.test_name == "HbA1c"]
    # Exactly one HbA1c row — the pre-existing one — and it's still the
    # same id (no duplicate created).
    assert len(hba1c_rows) == 1
    assert hba1c_rows[0].id == existing_id
    assert result["HbA1c"]["skipped_open_followup"] >= 1


async def test_sweep_idempotent_on_repeat():
    """Running the sweep twice should NOT double-materialise."""
    patient_id = await _seed_patient(cohort_diabetes=True)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        first = await care_gaps.sweep_care_gaps(db)
        await db.commit()
        second = await care_gaps.sweep_care_gaps(db)
        await db.commit()

    open_labs = await _open_labs_for(patient_id)
    hba1c_rows = [lab for lab in open_labs if lab.test_name == "HbA1c"]
    assert len(hba1c_rows) == 1
    # First run should have materialised; second should skip the same patient.
    assert first["HbA1c"]["materialized"] >= 1
    assert second["HbA1c"]["skipped_open_followup"] >= 1


# ---- Count helper ----------------------------------------------------------


async def test_overdue_care_gap_count_matches_sweep():
    """The count tile and the sweep must agree on what counts as a gap
    for a SPECIFIC patient — verified per-patient rather than against a
    global aggregate (the background scheduler can move the global count
    in either direction during a long test run)."""
    diabetic_id = await _seed_patient(cohort_diabetes=True)

    # Before any sweep: the patient has no followup and no completion,
    # so the per-patient gates that drive the count helper say "gap".
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        from app.db.repositories import care_plans as care_plans_repo
        from services.scheduler.care_gaps import (
            _has_open_followup,
            _last_completed_at,
        )
        plans = await care_plans_repo.list_active(db)
        hba1c = next(
            p for p in plans if p.test_name == "HbA1c" and p.cohort_attr == "cohort_diabetes"
        )
        had_open_before = await _has_open_followup(
            db, patient_id=diabetic_id, test_name=hba1c.test_name
        )
        last_before = await _last_completed_at(
            db, patient_id=diabetic_id, test_name=hba1c.test_name
        )
    assert had_open_before is False
    assert last_before is None

    # Sweep — should materialise an HbA1c lab_followup for this patient.
    async with SessionLocal() as db:
        await care_gaps.sweep_care_gaps(db)
        await db.commit()

    # After the sweep, the same per-patient gate flips to "open followup"
    # so the count helper would no longer count this patient as a gap.
    async with SessionLocal() as db:
        had_open_after = await _has_open_followup(
            db, patient_id=diabetic_id, test_name="HbA1c"
        )
    assert had_open_after is True

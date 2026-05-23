"""Integration tests for clinician-authored cohort tags + patient
assignments + the sweep using tag-based care plans.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.db.models import Patient
from app.db.repositories import care_plans as care_plans_repo
from app.db.repositories import cohort_tags as cohort_tags_repo
from app.db.session import get_sessionmaker
from services.scheduler import care_gaps

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping cohort_tag integration tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


def _unique_label() -> str:
    return f"Test Cohort {uuid.uuid4().hex[:6]}"


# ---- Repo ---------------------------------------------------------------


def test_slugify_basic_cases():
    assert cohort_tags_repo.slugify("Post-MI") == "post-mi"
    assert cohort_tags_repo.slugify("Pregnancy 3T") == "pregnancy-3t"
    # Multiple punctuation collapses to one separator.
    assert cohort_tags_repo.slugify("CKD ‧ Stage 3+") == "ckd-stage-3"
    # Empty / weird input falls back to "tag" so we never write a
    # NULL/empty slug to the DB.
    assert cohort_tags_repo.slugify("!!!") == "tag"


async def test_create_then_assign_then_remove():
    label = _unique_label()
    SessionLocal = get_sessionmaker()
    suffix = uuid.uuid4().hex[:8]
    async with SessionLocal() as db:
        tag = await cohort_tags_repo.create(db, label=label)
        patient = Patient(
            full_name=f"Tag Test {suffix}",
            phone=f"tag-test-{suffix}",
        )
        db.add(patient)
        await db.flush()
        await db.commit()
        tag_id = tag.id
        patient_id = patient.id

        # Assign + verify uniqueness on repeat assign.
        first = await cohort_tags_repo.assign(
            db, patient_id=patient_id, cohort_tag_id=tag_id
        )
        await db.commit()
        repeat = await cohort_tags_repo.assign(
            db, patient_id=patient_id, cohort_tag_id=tag_id
        )
        # Same row returned (idempotent).
        assert repeat.id == first.id

        # patients_for_tag includes the assigned patient.
        members = await cohort_tags_repo.patients_for_tag(db, tag_id)
        assert any(p.id == patient_id for p in members)

        removed = await cohort_tags_repo.remove(
            db, patient_id=patient_id, cohort_tag_id=tag_id
        )
        await db.commit()
        assert removed is True

        # Second remove is a no-op (False).
        again = await cohort_tags_repo.remove(
            db, patient_id=patient_id, cohort_tag_id=tag_id
        )
    assert again is False


# ---- Endpoints ---------------------------------------------------------


def test_endpoint_create_then_assign_then_remove(orchestrator_client):
    label = _unique_label()
    create = orchestrator_client.post(
        "/cohort-tags", json={"label": label, "description": "test"}
    )
    assert create.status_code == 200
    tag = create.json()
    assert tag["slug"]  # auto-generated
    assert tag["patient_count"] == 0
    tag_id = tag["id"]

    # Slug duplicate → 409.
    dup = orchestrator_client.post(
        "/cohort-tags",
        json={"label": label, "slug": tag["slug"]},
    )
    assert dup.status_code == 409

    # Need a patient to assign to.
    SessionLocal = get_sessionmaker()
    suffix = uuid.uuid4().hex[:8]

    async def _seed_patient() -> int:
        async with SessionLocal() as db:
            p = Patient(
                full_name=f"Endpoint Tag {suffix}",
                phone=f"endpoint-tag-{suffix}",
            )
            db.add(p)
            await db.flush()
            await db.commit()
            return p.id

    import asyncio

    patient_id = asyncio.get_event_loop().run_until_complete(_seed_patient())

    assign = orchestrator_client.post(
        f"/patients/{patient_id}/cohort-tags",
        json={"cohort_tag_id": tag_id, "assigned_by": "dr.smith"},
    )
    assert assign.status_code == 200
    body = assign.json()
    assert body["cohort_tag_label"] == label
    assert body["assigned_by"] == "dr.smith"

    # Listing reflects the assignment.
    listed = orchestrator_client.get(
        f"/patients/{patient_id}/cohort-tags"
    ).json()
    assert any(a["cohort_tag_id"] == tag_id for a in listed)

    # Re-assigning is idempotent (200, same row).
    second = orchestrator_client.post(
        f"/patients/{patient_id}/cohort-tags",
        json={"cohort_tag_id": tag_id},
    )
    assert second.status_code == 200
    assert second.json()["id"] == body["id"]

    # patient_count on the tag now reads 1.
    refreshed = orchestrator_client.get("/cohort-tags").json()
    target = next(t for t in refreshed if t["id"] == tag_id)
    assert target["patient_count"] >= 1

    # Remove via DELETE.
    delete = orchestrator_client.delete(
        f"/patients/{patient_id}/cohort-tags/{tag_id}"
    )
    assert delete.status_code == 204

    # Repeat delete now 404.
    delete_again = orchestrator_client.delete(
        f"/patients/{patient_id}/cohort-tags/{tag_id}"
    )
    assert delete_again.status_code == 404


def test_endpoint_assigning_inactive_tag_is_rejected(orchestrator_client):
    label = _unique_label()
    tag = orchestrator_client.post(
        "/cohort-tags", json={"label": label}
    ).json()

    # Deactivate the tag.
    orchestrator_client.put(
        f"/cohort-tags/{tag['id']}", json={"active": False}
    )

    SessionLocal = get_sessionmaker()
    suffix = uuid.uuid4().hex[:8]

    async def _seed_patient() -> int:
        async with SessionLocal() as db:
            p = Patient(
                full_name=f"Inactive Tag {suffix}",
                phone=f"inactive-tag-{suffix}",
            )
            db.add(p)
            await db.flush()
            await db.commit()
            return p.id

    import asyncio

    patient_id = asyncio.get_event_loop().run_until_complete(_seed_patient())

    r = orchestrator_client.post(
        f"/patients/{patient_id}/cohort-tags",
        json={"cohort_tag_id": tag["id"]},
    )
    assert r.status_code == 409


def test_cohort_picker_lists_legacy_and_tags(orchestrator_client):
    """The /care-plans/cohorts endpoint feeds the picker — must include
    the 3 legacy boolean cohorts AND every active tag."""
    # Create a fresh tag so we can assert it shows up.
    label = _unique_label()
    tag = orchestrator_client.post(
        "/cohort-tags", json={"label": label}
    ).json()

    options = orchestrator_client.get("/care-plans/cohorts").json()
    booleans = [o for o in options if o["kind"] == "boolean"]
    tags = [o for o in options if o["kind"] == "tag"]

    boolean_attrs = {o["cohort_attr"] for o in booleans}
    assert {"cohort_diabetes", "cohort_cardiac", "cohort_fall_risk"} <= boolean_attrs

    tag_ids = {o["cohort_tag_id"] for o in tags}
    assert tag["id"] in tag_ids


# ---- Care plans + sweep with tag-based cohort ---------------------------


def test_create_care_plan_against_tag(orchestrator_client):
    """Build a tag-based plan via the API; verify the DTO carries the
    tag label/slug for the UI."""
    label = _unique_label()
    tag = orchestrator_client.post(
        "/cohort-tags", json={"label": label}
    ).json()

    plan = orchestrator_client.post(
        "/care-plans",
        json={
            "cohort_tag_id": tag["id"],
            "test_name": f"Tag Test {uuid.uuid4().hex[:6]}",
            "cadence_days": 90,
        },
    ).json()
    assert plan["cohort_attr"] is None
    assert plan["cohort_tag_id"] == tag["id"]
    assert plan["cohort_tag_label"] == label
    assert plan["cohort_tag_slug"] == tag["slug"]


def test_create_care_plan_rejects_both_cohort_choices(orchestrator_client):
    """Pydantic + endpoint validation: pass both cohort_attr and
    cohort_tag_id → 400 (exactly-one required)."""
    label = _unique_label()
    tag = orchestrator_client.post(
        "/cohort-tags", json={"label": label}
    ).json()

    r = orchestrator_client.post(
        "/care-plans",
        json={
            "cohort_attr": "cohort_diabetes",
            "cohort_tag_id": tag["id"],
            "test_name": "ambiguous",
            "cadence_days": 90,
        },
    )
    assert r.status_code == 400


def test_create_care_plan_rejects_neither_cohort(orchestrator_client):
    r = orchestrator_client.post(
        "/care-plans",
        json={"test_name": "no cohort", "cadence_days": 90},
    )
    assert r.status_code == 400


async def test_sweep_materialises_for_tag_members():
    """End-to-end: create a tag + plan + assign one patient + run the
    sweep. The patient should get a lab_followup."""
    from app.db.models import FollowupStatus, LabFollowup
    from sqlalchemy import select

    SessionLocal = get_sessionmaker()
    suffix = uuid.uuid4().hex[:8]
    test_name = f"Sweep Test {suffix}"
    label = _unique_label()

    async with SessionLocal() as db:
        tag = await cohort_tags_repo.create(db, label=label)
        await db.commit()
        tag_id = tag.id

        plan = await care_plans_repo.create(
            db,
            cohort_tag_id=tag_id,
            test_name=test_name,
            cadence_days=90,
        )
        await db.commit()
        plan_id = plan.id

        patient = Patient(
            full_name=f"Sweep tag {suffix}",
            phone=f"sweep-tag-{suffix}",
        )
        db.add(patient)
        await db.flush()
        patient_id = patient.id

        await cohort_tags_repo.assign(
            db, patient_id=patient_id, cohort_tag_id=tag_id
        )
        await db.commit()

        result = await care_gaps.sweep_care_gaps(db)
        await db.commit()

    # Sweep keys by test_name; the new plan must show 1 materialised.
    assert result[test_name]["materialized"] >= 1

    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(LabFollowup).where(
                    LabFollowup.patient_id == patient_id,
                    LabFollowup.test_name == test_name,
                )
            )
        ).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == FollowupStatus.due
    # Note string carries the tag-based identifier (cohort tag id, not
    # plan id — the tag is what defines membership).
    assert f"tag#{tag_id}" in (rows[0].notes or "")

"""Integration tests for the doctor pre-visit summary endpoint.

Builds a patient with a known shape (cohort tag, regimen, lab followup,
ops ticket, exemption, classified inbox row, prior recap) then asserts
the aggregator returns each block joined and trimmed correctly. Read-
only endpoint, no clinical mutations.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.db.models import (
    Appointment,
    AppointmentRecap,
    AppointmentStatus,
    Doctor,
    DoctorOAuthStatus,
    FollowupStatus,
    LabFollowup,
    Patient,
    RecapStatus,
    Regimen,
)
from app.db.repositories import care_plan_exemptions as care_plan_exemptions_repo
from app.db.repositories import care_plans as care_plans_repo
from app.db.repositories import cohort_tags as cohort_tags_repo
from app.db.repositories import (
    inbound_classifications as inbound_classifications_repo,
)
from app.db.repositories import ops_tickets as ops_tickets_repo
from app.db.session import get_sessionmaker

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping pre-visit integration tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


async def _seed_full_pre_visit_fixture():
    """Build a patient + everything the pre-visit endpoint joins. Returns
    (appointment_id, patient_id, ticket_id, tag_id, exemption_id, inbox_row_id,
    prior_recap_appointment_id) so individual tests can assert on each."""
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        patient = Patient(
            full_name=f"Pre-visit Test {suffix}",
            phone=f"prevtest-{suffix}",
            cohort_diabetes=True,
        )
        doctor = Doctor(
            name=f"Dr Pre-visit {suffix}",
            email=f"dr-prev-{suffix}@example.com",
            timezone="UTC",
            calendar_id="primary",
            oauth_status=DoctorOAuthStatus.connected,
        )
        db.add_all([patient, doctor])
        await db.flush()

        # Active regimen.
        regimen = Regimen(
            patient_id=patient.id,
            medication_name="Metformin",
            dose="500 mg",
            schedule={
                "type": "times_of_day",
                "times": ["08:00", "20:00"],
                "timezone": "Asia/Kolkata",
            },
        )
        db.add(regimen)

        # Upcoming appointment (the one we'll query).
        appt_when = datetime.now(timezone.utc) + timedelta(hours=24)
        appointment = Appointment(
            patient_id=patient.id,
            doctor_id=doctor.id,
            scheduled_for=appt_when,
            end_at=appt_when + timedelta(minutes=30),
            status=AppointmentStatus.confirmed,
            source="test",
            summary="Pre-visit aggregator fixture",
        )
        db.add(appointment)

        # PRIOR appointment + sent recap so the aggregator surfaces it.
        prior_when = datetime.now(timezone.utc) - timedelta(days=14)
        prior_appt = Appointment(
            patient_id=patient.id,
            doctor_id=doctor.id,
            scheduled_for=prior_when,
            end_at=prior_when + timedelta(minutes=30),
            status=AppointmentStatus.completed,
            source="test",
        )
        db.add(prior_appt)
        await db.flush()

        prior_recap = AppointmentRecap(
            appointment_id=prior_appt.id,
            patient_id=patient.id,
            doctor_id=doctor.id,
            doctor_notes="Continue current plan.",
            structured_payload={},
            generated_text=(
                "Hi Pre-visit, here's a recap of your visit. "
                "Continue your Metformin and check HbA1c next quarter."
            ),
            status=RecapStatus.sent,
            sent_at=prior_when + timedelta(hours=1),
            sent_message_id="wamid.test.prior",
        )
        db.add(prior_recap)

        # Open lab followup (overdue) + an open ticket.
        lab = LabFollowup(
            patient_id=patient.id,
            test_name="HbA1c",
            status=FollowupStatus.due,
            due_by=(datetime.now(timezone.utc) - timedelta(days=2)).date(),
        )
        db.add(lab)
        await db.flush()

        ticket = await ops_tickets_repo.create(
            db,
            patient_id=patient.phone,
            category="lab_help",
            priority="p3",
            sla_minutes=1440,
            notes="patient asked for help scheduling",
        )

        # Cohort tag assignment.
        tag = await cohort_tags_repo.create(
            db, label=f"pre-visit-tag-{suffix}"
        )
        await db.flush()
        await cohort_tags_repo.assign(
            db, patient_id=patient.id, cohort_tag_id=tag.id
        )

        # Active exemption against an existing care plan (use a freshly
        # created tag-based plan so we don't pollute the seeded plans).
        exempt_plan = await care_plans_repo.create(
            db,
            cohort_tag_id=tag.id,
            test_name=f"pre-visit-test-{suffix}",
            cadence_days=180,
        )
        await db.flush()
        exemption = await care_plan_exemptions_repo.create(
            db,
            patient_id=patient.id,
            care_plan_id=exempt_plan.id,
            reason="alternate care under specialist",
        )

        # Inbox row.
        inbox_row = await inbound_classifications_repo.create(
            db,
            message_id=f"msg-{suffix}",
            patient_phone=patient.phone,
            patient_db_id=patient.id,
            inbound_text="My blood sugar reading was high yesterday",
            category="clinical_question",
            summary="Patient reports elevated blood sugar yesterday.",
            urgency="medium",
            handler_used="llm_compose",
            response_text="I've passed this to your care team.",
            escalated=False,
            ticket_id=None,
            input_kind="text",
        )

        await db.commit()
        return {
            "appointment_id": appointment.id,
            "patient_id": patient.id,
            "patient_phone": patient.phone,
            "doctor_id": doctor.id,
            "ticket_id": ticket.id,
            "tag_id": tag.id,
            "exemption_id": exemption.id,
            "inbox_row_id": inbox_row.id,
            "prior_recap_appointment_id": prior_appt.id,
        }


# ---- Endpoint coverage --------------------------------------------------


async def test_pre_visit_summary_aggregates_full_patient_state(
    orchestrator_client,
):
    fx = await _seed_full_pre_visit_fixture()

    r = orchestrator_client.get(
        f"/appointments/{fx['appointment_id']}/pre-visit"
    )
    assert r.status_code == 200
    body = r.json()

    # Header
    assert body["appointment"]["id"] == fx["appointment_id"]
    assert body["patient"]["id"] == fx["patient_id"]
    assert body["patient"]["full_name"].startswith("Pre-visit Test")
    assert "cohort_diabetes" in body["cohort_flags"]
    assert any(
        t["cohort_tag_id"] == fx["tag_id"] for t in body["cohort_tags"]
    )

    # Regimens
    regimen_names = [r["medication_name"] for r in body["regimens"]]
    assert "Metformin" in regimen_names

    # Open lab followups — one overdue HbA1c.
    lab_tests = [l["test_name"] for l in body["open_lab_followups"]]
    assert "HbA1c" in lab_tests
    overdue_labs = [
        l for l in body["open_lab_followups"] if l["is_overdue"]
    ]
    assert len(overdue_labs) >= 1

    # Open tickets
    ticket_ids = [t["ticket_id"] for t in body["open_tickets"]]
    assert str(fx["ticket_id"]) in ticket_ids

    # Active exemption
    exemption_ids = [ex["id"] for ex in body["active_exemptions"]]
    assert fx["exemption_id"] in exemption_ids

    # Recent inbox
    inbox_ids = [i["id"] for i in body["recent_inbox"]]
    assert fx["inbox_row_id"] in inbox_ids
    inbox_match = next(
        i for i in body["recent_inbox"] if i["id"] == fx["inbox_row_id"]
    )
    assert inbox_match["category"] == "clinical_question"
    assert inbox_match["urgency"] == "medium"

    # Last recap excerpt — the PRIOR appointment's recap, NOT this one.
    assert body["last_recap"] is not None
    assert (
        body["last_recap"]["appointment_id"]
        == fx["prior_recap_appointment_id"]
    )
    assert "Continue your Metformin" in body["last_recap"]["summary"]


async def test_pre_visit_excludes_current_appointments_recap(
    orchestrator_client,
):
    """Ensure the prior-recap excerpt skips a recap on the SAME
    appointment we're previewing — that one's the post-visit, not
    the pre-visit context."""
    fx = await _seed_full_pre_visit_fixture()

    # Add a recap on the SAME appointment we're querying — it should
    # NOT be returned as last_recap.
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        recap = AppointmentRecap(
            appointment_id=fx["appointment_id"],
            patient_id=fx["patient_id"],
            # Use the fixture's actual doctor — hardcoded ``1``
            # only worked when migration-seeded doctor #1 was
            # present in the test DB.
            doctor_id=fx["doctor_id"],
            structured_payload={},
            generated_text="this is the CURRENT visit's recap",
            status=RecapStatus.sent,
            sent_at=datetime.now(timezone.utc),
        )
        db.add(recap)
        await db.flush()
        await db.commit()

    body = orchestrator_client.get(
        f"/appointments/{fx['appointment_id']}/pre-visit"
    ).json()
    if body["last_recap"] is not None:
        assert body["last_recap"]["appointment_id"] != fx["appointment_id"]


def test_pre_visit_404_for_unknown_appointment(orchestrator_client):
    r = orchestrator_client.get("/appointments/999999999/pre-visit")
    assert r.status_code == 404


async def test_pre_visit_handles_minimal_patient(orchestrator_client):
    """Patient with no regimens, no inbox, no tickets — endpoint must
    return cleanly with empty arrays, not 500."""
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        patient = Patient(
            full_name=f"Minimal {suffix}",
            phone=f"min-{suffix}",
        )
        doctor = Doctor(
            name=f"Dr Min {suffix}",
            email=f"dr-min-{suffix}@example.com",
            timezone="UTC",
            calendar_id="primary",
            oauth_status=DoctorOAuthStatus.connected,
        )
        db.add_all([patient, doctor])
        await db.flush()
        when = datetime.now(timezone.utc) + timedelta(hours=2)
        appt = Appointment(
            patient_id=patient.id,
            doctor_id=doctor.id,
            scheduled_for=when,
            end_at=when + timedelta(minutes=30),
            status=AppointmentStatus.confirmed,
            source="test",
        )
        db.add(appt)
        await db.flush()
        await db.commit()
        appt_id = appt.id

    r = orchestrator_client.get(f"/appointments/{appt_id}/pre-visit")
    assert r.status_code == 200
    body = r.json()
    assert body["regimens"] == []
    assert body["open_lab_followups"] == []
    assert body["open_tickets"] == []
    assert body["active_exemptions"] == []
    assert body["recent_inbox"] == []
    assert body["last_recap"] is None
    assert body["has_caregiver_cc"] is False
    assert body["adherence"]["total"] == 0

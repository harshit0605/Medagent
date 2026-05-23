"""Right-of-erasure compatibility for V2-added tables (slice 15).

Slices 6, 8, 10, 14 introduced new tables that snapshot patient
PII (verbatim quotes, LLM-summarised descriptions, phone-string
mirrors). The erasure flow was extended in
``services/orchestrator/patient_erasure.py`` to scrub each;
these tests confirm each surface is wiped end-to-end.

Test pattern: seed → erase → verify the post-erasure DB state
no longer carries the PII that was there pre-erasure.

DATABASE_URL required.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.db.models import (
    BroadcastSend,
    CarePlanGoal,
    ClinicalAlert,
    MetricObservation,
    Patient,
    VisitBrief,
)
from app.db.repositories import (
    broadcast_campaigns as broadcast_campaigns_repo,
    care_plan_goals as goals_repo,
    clinical_alerts as clinical_alerts_repo,
    visit_briefs as visit_briefs_repo,
)
from app.db.session import get_sessionmaker
from services.orchestrator import patient_erasure

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping V2 erasure tests",
)


# ---- helpers --------------------------------------------------------------


async def _seed_patient_with_v2_pii() -> tuple[int, str]:
    """Create a patient + a row in every V2 PII-bearing table
    so the erasure flow has something to scrub."""
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(
            full_name=f"V2 Erase {suffix}",
            phone=f"v2erase-{suffix}",
            consent_sms=True,
        )
        db.add(p)
        await db.flush()

        # Clinical alert (slice 10) — verbatim quote + LLM
        # summary + red flags.
        await clinical_alerts_repo.create(
            db,
            patient_id=p.id,
            patient_phone=p.phone,
            message_id=None,
            severity="critical",
            red_flags=["chest pain", "shortness of breath"],
            clinical_summary=(
                "Patient reports active chest pain at rest "
                "with breathing difficulty"
            ),
            inbound_text=(
                "I have severe chest pain and can't breathe properly"
            ),
            llm_model="gpt-4o-mini",
        )

        # Visit brief (slice 8) — LLM prose + structured
        # talking points + red flags + metrics.
        await visit_briefs_repo.create(
            db,
            patient_id=p.id,
            appointment_id=None,
            doctor_id=None,
            window_start=datetime.now(timezone.utc),
            window_end=datetime.now(timezone.utc),
            llm_model="gpt-4o-mini",
            prompt_tokens=100,
            completion_tokens=80,
            summary=(
                "Patient reported new side-effect of nausea and "
                "missed two doses last week."
            ),
            talking_points=[
                "Discuss adherence with metformin",
                "Ask about evening headaches",
            ],
            red_flags=["recent missed doses"],
            key_metrics={"adherence_rate": 0.7, "doses_missed": 3},
            generated_by="ops",
        )

        # Broadcast send (slice 6) — phone-string snapshot in
        # ``patient_id``. Need a campaign first.
        campaign = await broadcast_campaigns_repo.create(
            db,
            name=f"v2-erase-test-{suffix}",
            template_name="test_template_v1",
            template_params={},
            cohort_filter={"cohort": "diabetes"},
            created_by="ops",
        )
        await broadcast_campaigns_repo.add_send(
            db,
            campaign_id=campaign.id,
            patient_id=p.phone,
            patient_db_id=p.id,
            status="pending",
            skip_reason=None,
            scheduled_event_id=None,
        )

        # Care plan goal + observation (slice 14) — both have
        # free-form ``notes`` columns that may carry PII.
        goal = await goals_repo.create_goal(
            db,
            patient_id=p.id,
            metric_key="hba1c",
            metric_label="HbA1c",
            target_value=Decimal("7.0"),
            comparator="less_than",
            target_unit="%",
            created_by="ops",
            notes="patient mentioned family history of diabetes",
        )
        await goals_repo.record_observation(
            db,
            patient_id=p.id,
            goal_id=goal.id,
            metric_key="hba1c",
            value=Decimal("7.4"),
            unit="%",
            source="lab",
            recorded_by="ops",
            notes="lab drawn after dinner with patient's family",
        )

        await db.commit()
        return p.id, p.phone


# ---- per-table scrub tests ------------------------------------------------


async def test_erasure_scrubs_clinical_alerts():
    """Verbatim patient quote in ``inbound_text`` + LLM summary
    + red-flag list + resolution notes must all be wiped.
    Severity / status stay so historical aggregate queries
    keep working."""
    pid, phone = await _seed_patient_with_v2_pii()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await patient_erasure.erase_patient_data(
            db, patient_id=pid, actor="ops", reason="test"
        )
        await db.commit()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows = list(
            (
                await db.execute(
                    select(ClinicalAlert).where(
                        ClinicalAlert.patient_id == pid
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) >= 1
    for row in rows:
        assert row.inbound_text == "[erased]"
        assert row.clinical_summary == "[erased]"
        assert row.red_flags == []
        assert row.resolution_notes is None
        # Phone snapshot rotated to the anonymized form.
        assert row.patient_phone is None or row.patient_phone.startswith("erased:")
        # Severity + status retained for aggregate queries.
        assert row.severity == "critical"
        assert row.status == "open"


async def test_erasure_scrubs_visit_briefs():
    """LLM-generated summary + talking points + red flags +
    metrics are all derived from PII and must be wiped."""
    pid, _ = await _seed_patient_with_v2_pii()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await patient_erasure.erase_patient_data(
            db, patient_id=pid, actor="ops", reason="test"
        )
        await db.commit()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows = list(
            (
                await db.execute(
                    select(VisitBrief).where(
                        VisitBrief.patient_id == pid
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) >= 1
    for row in rows:
        assert row.summary == "[erased]"
        assert row.talking_points == []
        assert row.red_flags == []
        assert row.key_metrics == {}
        # llm_model + token counts kept for cost audit.
        assert row.llm_model is not None


async def test_erasure_rotates_broadcast_send_phone_snapshot():
    """The ``patient_id`` column on broadcast_sends is a phone-
    string snapshot from materialisation time. Erasure rotates
    it to the anonymized phone so the campaign progress page
    can still count the row without leaking the original."""
    pid, original_phone = await _seed_patient_with_v2_pii()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await patient_erasure.erase_patient_data(
            db, patient_id=pid, actor="ops", reason="test"
        )
        await db.commit()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows = list(
            (
                await db.execute(
                    select(BroadcastSend).where(
                        BroadcastSend.patient_db_id == pid
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) >= 1
    for row in rows:
        assert row.patient_id != original_phone
        assert row.patient_id.startswith("erased:")


async def test_erasure_clears_metric_observation_notes():
    """The free-form notes column on metric_observations and
    care_plan_goals may carry the patient's words ('lab drawn
    after dinner with patient's family'). Wipe both. Numeric
    values + metric_key stay so anonymized clinical research
    is still possible."""
    pid, _ = await _seed_patient_with_v2_pii()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await patient_erasure.erase_patient_data(
            db, patient_id=pid, actor="ops", reason="test"
        )
        await db.commit()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        obs_rows = list(
            (
                await db.execute(
                    select(MetricObservation).where(
                        MetricObservation.patient_id == pid
                    )
                )
            )
            .scalars()
            .all()
        )
        goal_rows = list(
            (
                await db.execute(
                    select(CarePlanGoal).where(
                        CarePlanGoal.patient_id == pid
                    )
                )
            )
            .scalars()
            .all()
        )

    assert len(obs_rows) >= 1
    for row in obs_rows:
        assert row.notes is None
        # Numeric value retained for aggregate research.
        assert row.value is not None
        assert row.metric_key == "hba1c"

    assert len(goal_rows) >= 1
    for row in goal_rows:
        assert row.notes is None
        # Goal target retained.
        assert row.metric_key == "hba1c"
        assert row.target_value == Decimal("7.000")


async def test_erasure_idempotent_against_v2_tables():
    """Re-running erasure on an already-erased patient must
    not change the V2 tables — they should stay scrubbed.
    Defends against ops accidentally clicking 'Erase' twice."""
    pid, _ = await _seed_patient_with_v2_pii()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await patient_erasure.erase_patient_data(
            db, patient_id=pid, actor="ops", reason="test"
        )
        await db.commit()

    # Capture state after first erasure.
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        first = list(
            (
                await db.execute(
                    select(ClinicalAlert).where(
                        ClinicalAlert.patient_id == pid
                    )
                )
            )
            .scalars()
            .all()
        )
    first_phones = {r.patient_phone for r in first}

    # Re-erase. The function is idempotent — it returns the
    # already-erased patient row without re-running step 1+
    # (the V2 steps, however, would still be safe to re-run
    # because they're idempotent UPDATEs to the placeholder
    # values, but the function short-circuits before that).
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await patient_erasure.erase_patient_data(
            db, patient_id=pid, actor="ops", reason="test"
        )
        await db.commit()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        second = list(
            (
                await db.execute(
                    select(ClinicalAlert).where(
                        ClinicalAlert.patient_id == pid
                    )
                )
            )
            .scalars()
            .all()
        )
    second_phones = {r.patient_phone for r in second}
    # State must be stable across re-erasure.
    assert first_phones == second_phones
    for row in second:
        assert row.inbound_text == "[erased]"


async def test_erasure_preserves_audit_evidence_in_v2_tables():
    """Severity + status on clinical_alerts, llm_model + token
    counts on visit_briefs, target_value on care_plan_goals
    are NOT PII — they're aggregate research signals + cost
    audit. Erasure must NOT wipe them."""
    pid, _ = await _seed_patient_with_v2_pii()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await patient_erasure.erase_patient_data(
            db, patient_id=pid, actor="ops", reason="test"
        )
        await db.commit()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        alert = (
            (
                await db.execute(
                    select(ClinicalAlert).where(
                        ClinicalAlert.patient_id == pid
                    )
                )
            )
            .scalars()
            .first()
        )
        brief = (
            (
                await db.execute(
                    select(VisitBrief).where(
                        VisitBrief.patient_id == pid
                    )
                )
            )
            .scalars()
            .first()
        )
        goal = (
            (
                await db.execute(
                    select(CarePlanGoal).where(
                        CarePlanGoal.patient_id == pid
                    )
                )
            )
            .scalars()
            .first()
        )

    assert alert is not None
    assert alert.severity == "critical"
    assert alert.status == "open"

    assert brief is not None
    assert brief.llm_model is not None
    assert brief.prompt_tokens is not None

    assert goal is not None
    assert goal.target_value == Decimal("7.000")
    assert goal.metric_key == "hba1c"
    assert goal.comparator == "less_than"

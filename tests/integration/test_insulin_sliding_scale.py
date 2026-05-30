"""Integration: glucose self-report → sliding-scale insulin recommendation.

When a diabetic patient with an active sliding-scale insulin regimen logs a
glucose reading, the vitals reply appends a care-team-defined dose suggestion
(advisory, never auto-administered). Hypo / severe-hyper escalate.
"""

from __future__ import annotations

import os
import uuid

import pytest

from app.db.models import Patient, Regimen
from app.db.session import get_sessionmaker
from services.orchestrator import vitals_handler

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping integration tests",
)


_SCALE = {
    "kind": "sliding_scale",
    "unit": "units",
    "bands": [
        {"min": 0, "max": 149, "units": 0},
        {"min": 150, "max": 199, "units": 2},
        {"min": 200, "max": 249, "units": 4},
        {"min": 250, "max": 299, "units": 6},
        {"min": 300, "max": 9999, "units": 8},
    ],
    "low_glucose_threshold": 70,
    "high_glucose_escalate": 400,
}


async def _seed(*, with_scale: bool) -> str:
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(
            full_name=f"Insulin {suffix}",
            phone=f"insulin-{suffix}",
            consent_sms=True,
            cohort_diabetes=True,
        )
        db.add(p)
        await db.flush()
        reg = Regimen(
            patient_id=p.id,
            medication_name="Insulin (rapid-acting)",
            dose="per sliding scale",
            schedule={"kind": "fixed", "times": ["08:00"]},
            dosing_rule=_SCALE if with_scale else None,
        )
        db.add(reg)
        await db.commit()
        return p.phone


async def test_glucose_with_scale_appends_recommendation():
    phone = await _seed(with_scale=True)
    delta = await vitals_handler.handle_vitals_log(
        patient_phone=phone, new_user_text="sugar 220"
    )
    assert delta is not None
    body = delta["response_body"]
    assert "sliding scale suggests 4 unit" in body
    assert "confirm with your care team" in body.lower()
    assert "insulin_sliding_scale_suggested" in delta["audit_reasons"]
    assert "insulin_safety_escalate" not in delta["audit_reasons"]


async def test_hypo_glucose_warns_and_escalates():
    phone = await _seed(with_scale=True)
    delta = await vitals_handler.handle_vitals_log(
        patient_phone=phone, new_user_text="sugar 55"
    )
    assert delta is not None
    body = delta["response_body"].lower()
    assert "too low" in body
    assert "insulin_safety_escalate" in delta["audit_reasons"]
    # No unit suggestion when hypo.
    assert "suggests" not in body


async def test_severe_hyper_doses_top_band_and_escalates():
    phone = await _seed(with_scale=True)
    delta = await vitals_handler.handle_vitals_log(
        patient_phone=phone, new_user_text="sugar 450"
    )
    assert delta is not None
    assert "suggests 8 unit" in delta["response_body"]
    assert "insulin_safety_escalate" in delta["audit_reasons"]
    assert "urgent" in delta["response_body"].lower()


async def test_no_scale_regimen_no_recommendation():
    phone = await _seed(with_scale=False)
    delta = await vitals_handler.handle_vitals_log(
        patient_phone=phone, new_user_text="sugar 220"
    )
    assert delta is not None
    assert "sliding scale" not in delta["response_body"].lower()
    assert "insulin_sliding_scale_suggested" not in delta["audit_reasons"]

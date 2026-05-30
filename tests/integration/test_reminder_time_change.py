"""Integration: self-service reminder-time change (G1)."""

from __future__ import annotations

import os
import uuid

import pytest

from app.db.models import Patient, Regimen
from app.db.repositories import regimens as regimens_repo
from app.db.session import get_sessionmaker
from services.orchestrator import reminder_time_handler as rt

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping integration tests",
)


async def _seed(*, n_regimens: int, times=None) -> tuple[int, str, list[int]]:
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    ids = []
    async with SessionLocal() as db:
        p = Patient(
            full_name=f"Retime {suffix}",
            phone=f"retime-{suffix}",
            consent_sms=True,
        )
        db.add(p)
        await db.flush()
        for i in range(n_regimens):
            reg = Regimen(
                patient_id=p.id,
                medication_name=f"Med{i}",
                dose="1 tab",
                schedule={
                    "type": "times_of_day",
                    "times": times or ["08:00"],
                    "timezone": "Asia/Kolkata",
                },
            )
            db.add(reg)
            await db.flush()
            ids.append(reg.id)
        await db.commit()
        return p.id, p.phone, ids


async def test_single_regimen_retimes_and_confirms():
    _pid, phone, ids = await _seed(n_regimens=1)
    delta = await rt.handle_time_change(
        patient_phone=phone, new_user_text="change my reminder to 9am"
    )
    assert delta is not None
    assert "reminder_time_changed" in delta["audit_reasons"]
    assert "09:00" in delta["response_body"]

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        reg = await regimens_repo.get(db, ids[0])
    assert reg.schedule["times"] == ["09:00"]


async def test_multiple_regimens_asks_which():
    _pid, phone, _ids = await _seed(n_regimens=2)
    delta = await rt.handle_time_change(
        patient_phone=phone, new_user_text="change my reminder to 9am"
    )
    assert delta is not None
    assert "reminder_time_change_ambiguous" in delta["audit_reasons"]
    assert "which one" in delta["response_body"].lower()


async def test_multidose_regimen_defers_to_ops():
    _pid, phone, _ids = await _seed(
        n_regimens=1, times=["08:00", "20:00"]
    )
    delta = await rt.handle_time_change(
        patient_phone=phone, new_user_text="change my reminder to 9am"
    )
    assert delta is not None
    assert "reminder_time_change_multidose" in delta["audit_reasons"]


async def test_no_regimen_replies_gracefully():
    _pid, phone, _ids = await _seed(n_regimens=0)
    delta = await rt.handle_time_change(
        patient_phone=phone, new_user_text="change my reminder to 9am"
    )
    assert delta is not None
    assert "reminder_time_change_no_regimen" in delta["audit_reasons"]

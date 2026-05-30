"""Integration: conversational pregnancy intake + data-aware status (E5/E6)."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.db.models import Patient
from app.db.repositories import pregnancies as pregnancies_repo
from app.db.session import get_sessionmaker
from services.orchestrator import pregnancy_nl_handler as nl

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping integration tests",
)


async def _seed_patient() -> tuple[int, str]:
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(
            full_name=f"Preg NL {suffix}",
            phone=f"pregnl-{suffix}",
            consent_sms=True,
        )
        db.add(p)
        await db.flush()
        await db.commit()
        return p.id, p.phone


async def test_nl_intake_opens_pregnancy(monkeypatch):
    pid, phone = await _seed_patient()
    # LMP ~10 weeks ago so the reply has future milestones.
    lmp = (datetime.now(timezone.utc).date() - timedelta(weeks=10))
    text = f"I'm pregnant, LMP {lmp.strftime('%d %b')}"

    delta = await nl.handle_pregnancy_nl(patient_phone=phone, new_user_text=text)
    assert delta is not None
    assert "pregnancy_nl_intake" in delta["audit_reasons"]
    assert "weeks along" in delta["response_body"].lower()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        preg = await pregnancies_repo.get_active_for_patient(db, pid)
        p = await db.get(Patient, pid)
    assert preg is not None
    assert p.cohort_pregnancy is True


async def test_status_query_is_data_aware():
    pid, phone = await _seed_patient()
    lmp = datetime.now(timezone.utc).date() - timedelta(weeks=12)
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await pregnancies_repo.create(db, patient_id=pid, lmp_date=lmp)
        await db.commit()

    delta = await nl.handle_pregnancy_status(
        patient_phone=phone, new_user_text="how many weeks am I?"
    )
    assert delta is not None
    body = delta["response_body"].lower()
    # ~12 weeks → trimester 1, and a next milestone phrase.
    assert "12 weeks" in body or "11 weeks" in body or "13 weeks" in body
    assert "next up" in body
    assert "pregnancy_status_query" in delta["audit_reasons"]


async def test_status_query_without_record_prompts_intake():
    _pid, phone = await _seed_patient()
    delta = await nl.handle_pregnancy_status(
        patient_phone=phone, new_user_text="what's next in my pregnancy?"
    )
    assert delta is not None
    assert "pregnancy_status_no_record" in delta["audit_reasons"]
    assert "last period" in delta["response_body"].lower()


async def test_intake_when_already_pregnant_returns_status():
    pid, phone = await _seed_patient()
    lmp = datetime.now(timezone.utc).date() - timedelta(weeks=8)
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await pregnancies_repo.create(db, patient_id=pid, lmp_date=lmp)
        await db.commit()

    # A second intake attempt → already-set-up status reply, not a dup.
    delta = await nl.handle_pregnancy_intake(
        patient_phone=phone,
        new_user_text=f"pregnant, LMP {lmp.strftime('%d %b')}",
    )
    assert delta is not None
    assert "already set up" in delta["response_body"].lower()

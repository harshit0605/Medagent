"""Multi-language template selection (I3).

The dispatcher sets the WhatsApp template language from the patient's
preferred_language so Meta serves the matching language version.
"""

from __future__ import annotations

import os
import uuid

import pytest

from app.db.models import Patient
from app.db.session import get_sessionmaker
from services.scheduler import dispatcher

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping integration tests",
)


async def _seed(*, language: str | None) -> str:
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(
            full_name=f"Lang {suffix}",
            phone=f"lang-{suffix}",
            consent_sms=True,
            preferred_language=language,
        )
        db.add(p)
        await db.flush()
        await db.commit()
        return p.phone


async def test_patient_language_returns_preference():
    phone = await _seed(language="hi")
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        lang = await dispatcher._patient_language(db, phone)
    assert lang == "hi"


async def test_patient_language_defaults_to_en():
    # preferred_language is NOT NULL default "en" — an unset patient gets "en"
    # (Meta serves the English template version), not None.
    phone = await _seed(language=None)
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        lang = await dispatcher._patient_language(db, phone)
    assert lang == "en"


async def test_patient_language_none_for_unknown_phone():
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        lang = await dispatcher._patient_language(db, "not-a-patient-xyz")
    assert lang is None


def test_message_out_carries_language():
    from shared.contracts.models import MessageOut

    m = MessageOut(
        patient_id="x",
        use_template=True,
        template_name="dose_reminder_v1",
        language="ta",
    )
    dumped = m.model_dump()
    assert dumped["language"] == "ta"

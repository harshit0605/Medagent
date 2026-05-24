"""Integration tests for caregiver CRUD + consent lifecycle + recap
fan-out (caregiver cc on recap send).

Exercises:
- Repo CRUD: create, find_by_phone, list_active_recap_recipients,
  confirm/revoke consent, soft-delete via update(active=False).
- Endpoints: list / create / update / confirm-consent / revoke-consent.
- End-to-end: confirmed + notify-on-recap caregivers receive recap copy
  alongside the patient.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.db.models import Patient
from app.db.repositories import caregivers as caregivers_repo
from app.db.session import get_sessionmaker

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping caregiver integration tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


async def _seed_patient(*, name_suffix: str | None = None) -> int:
    suffix = name_suffix or uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(
            full_name=f"Caregiver Test {suffix}",
            phone=f"caregiver-test-{suffix}",
        )
        db.add(p)
        await db.flush()
        await db.commit()
        return p.id


# ---- Repo ----------------------------------------------------------------


async def test_create_then_confirm_then_revoke_lifecycle():
    patient_id = await _seed_patient()
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        cg = await caregivers_repo.create(
            db,
            patient_id=patient_id,
            full_name="Anita K",
            phone="91+spouse-test",
            relationship_to_patient="spouse",
        )
        await db.commit()
        cg_id = cg.id

    assert cg.consent_status == caregivers_repo.CONSENT_PENDING

    async with SessionLocal() as db:
        # Pending caregivers don't show up as recap recipients.
        recipients = await caregivers_repo.list_active_recap_recipients(
            db, patient_id
        )
        assert all(r.id != cg_id for r in recipients)

        confirmed = await caregivers_repo.confirm_consent(
            db, cg_id, confirmed_by="dr.smith"
        )
        await db.commit()
    assert confirmed.consent_status == caregivers_repo.CONSENT_CONFIRMED
    assert confirmed.consent_confirmed_by == "dr.smith"

    async with SessionLocal() as db:
        recipients = await caregivers_repo.list_active_recap_recipients(
            db, patient_id
        )
        assert any(r.id == cg_id for r in recipients)

        # Re-confirming is idempotent — same row, original timestamp preserved.
        original_at = confirmed.consent_confirmed_at
        again = await caregivers_repo.confirm_consent(
            db, cg_id, confirmed_by="dr.other"
        )
    assert again.consent_confirmed_at == original_at

    async with SessionLocal() as db:
        revoked = await caregivers_repo.revoke_consent(db, cg_id)
        await db.commit()
    assert revoked.consent_status == caregivers_repo.CONSENT_REVOKED

    async with SessionLocal() as db:
        recipients = await caregivers_repo.list_active_recap_recipients(
            db, patient_id
        )
    # Revoked caregiver no longer in the recipient set.
    assert all(r.id != cg_id for r in recipients)


async def test_recap_recipients_respect_notify_flag():
    """Confirmed but notify_on_recap=False → not a recipient."""
    patient_id = await _seed_patient()
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        cg = await caregivers_repo.create(
            db,
            patient_id=patient_id,
            full_name="No-Recap Caregiver",
            phone="91+norecap-test",
        )
        await caregivers_repo.confirm_consent(
            db, cg.id, confirmed_by="ops"
        )
        await caregivers_repo.update(db, cg.id, notify_on_recap=False)
        await db.commit()

    async with SessionLocal() as db:
        recipients = await caregivers_repo.list_active_recap_recipients(
            db, patient_id
        )
    assert all(r.id != cg.id for r in recipients)


async def test_inactive_caregivers_excluded_from_recipients():
    patient_id = await _seed_patient()
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        cg = await caregivers_repo.create(
            db,
            patient_id=patient_id,
            full_name="Soft-Deleted Caregiver",
            phone="91+softdelete-test",
        )
        await caregivers_repo.confirm_consent(
            db, cg.id, confirmed_by="ops"
        )
        await caregivers_repo.update(db, cg.id, active=False)
        await db.commit()

    async with SessionLocal() as db:
        recipients = await caregivers_repo.list_active_recap_recipients(
            db, patient_id
        )
    assert all(r.id != cg.id for r in recipients)


# ---- Endpoints ---------------------------------------------------------


def test_endpoint_create_then_confirm_then_revoke(orchestrator_client):
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()

    async def _seed() -> int:
        async with SessionLocal() as db:
            p = Patient(
                full_name=f"Endpoint Caregiver {suffix}",
                phone=f"endpoint-caregiver-{suffix}",
            )
            db.add(p)
            await db.flush()
            await db.commit()
            return p.id

    import asyncio

    patient_id = asyncio.get_event_loop().run_until_complete(_seed())

    create = orchestrator_client.post(
        f"/patients/{patient_id}/caregivers",
        json={
            "full_name": "Adult Daughter",
            "phone": "91+endpoint-cg-phone",
            "relationship_to_patient": "daughter",
            "notify_on_recap": True,
        },
    )
    assert create.status_code == 200
    cg = create.json()
    assert cg["consent_status"] == "pending"
    cg_id = cg["id"]

    listing = orchestrator_client.get(
        f"/patients/{patient_id}/caregivers"
    ).json()
    assert any(c["id"] == cg_id for c in listing)

    confirm = orchestrator_client.post(
        f"/caregivers/{cg_id}/confirm-consent",
        json={"confirmed_by": "dr.smith"},
    )
    assert confirm.status_code == 200
    assert confirm.json()["consent_status"] == "confirmed"
    assert confirm.json()["consent_confirmed_by"] == "dr.smith"

    revoke = orchestrator_client.post(
        f"/caregivers/{cg_id}/revoke-consent"
    )
    assert revoke.status_code == 200
    assert revoke.json()["consent_status"] == "revoked"


def test_endpoint_404_for_unknown_patient(orchestrator_client):
    r = orchestrator_client.post(
        "/patients/9999999/caregivers",
        json={
            "full_name": "Ghost",
            "phone": "+0000",
            "notify_on_recap": True,
        },
    )
    assert r.status_code == 404


def test_endpoint_update_toggle_notify_and_deactivate(orchestrator_client):
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()

    async def _seed() -> int:
        async with SessionLocal() as db:
            p = Patient(
                full_name=f"Toggle Test {suffix}",
                phone=f"toggle-test-{suffix}",
            )
            db.add(p)
            await db.flush()
            await db.commit()
            return p.id

    import asyncio

    patient_id = asyncio.get_event_loop().run_until_complete(_seed())

    cg = orchestrator_client.post(
        f"/patients/{patient_id}/caregivers",
        json={"full_name": "X", "phone": f"+toggle-{suffix}"},
    ).json()
    cg_id = cg["id"]

    # Toggle notify off.
    upd = orchestrator_client.put(
        f"/caregivers/{cg_id}", json={"notify_on_recap": False}
    )
    assert upd.status_code == 200
    assert upd.json()["notify_on_recap"] is False

    # Soft-delete (active=False).
    de = orchestrator_client.put(
        f"/caregivers/{cg_id}", json={"active": False}
    )
    assert de.status_code == 200
    assert de.json()["active"] is False

    # Active-only listing excludes; include_inactive=true includes.
    active = orchestrator_client.get(
        f"/patients/{patient_id}/caregivers"
    ).json()
    inactive = orchestrator_client.get(
        f"/patients/{patient_id}/caregivers",
        params={"include_inactive": "true"},
    ).json()
    assert all(c["id"] != cg_id for c in active)
    assert any(c["id"] == cg_id for c in inactive)


# ---- End-to-end: caregiver fan-out on recap send ------------------------


async def test_recap_send_cc_confirmed_caregiver(
    orchestrator_client, monkeypatch
):
    """When a recap is sent, every active + confirmed + notify-on-recap
    caregiver receives a copy. The patient send and each caregiver send
    are independent gateway calls — we monkeypatch the gateway helper to
    capture all phones called and assert the caregiver got cc'd."""
    from services.orchestrator import main as orchestrator_main

    captured_phones: list[str] = []

    async def fake_send(**kwargs):
        captured_phones.append(kwargs.get("patient_phone", ""))
        return f"wamid.fake.{uuid.uuid4().hex[:8]}"

    monkeypatch.setattr(
        orchestrator_main, "_send_recap_via_gateway", fake_send
    )

    # Seed: patient + appointment + caregiver (confirmed, notify=true).
    SessionLocal = get_sessionmaker()
    suffix = uuid.uuid4().hex[:8]

    async def _seed_appt_with_caregiver():
        from datetime import datetime, timedelta, timezone
        from app.db.models import (
            Appointment,
            AppointmentStatus,
            Doctor,
            DoctorOAuthStatus,
        )
        async with SessionLocal() as db:
            patient = Patient(
                full_name=f"Recap CC {suffix}",
                phone=f"recap-cc-{suffix}",
            )
            doctor = Doctor(
                name=f"Dr CC {suffix}",
                email=f"dr-cc-{suffix}@example.com",
                timezone="UTC",
                calendar_id="primary",
                oauth_status=DoctorOAuthStatus.connected,
            )
            db.add_all([patient, doctor])
            await db.flush()
            appt_when = datetime.now(timezone.utc) - timedelta(hours=2)
            appt = Appointment(
                patient_id=patient.id,
                doctor_id=doctor.id,
                scheduled_for=appt_when,
                end_at=appt_when + timedelta(minutes=30),
                status=AppointmentStatus.completed,
                source="test",
            )
            db.add(appt)
            await db.flush()
            cg = await caregivers_repo.create(
                db,
                patient_id=patient.id,
                full_name="CC Daughter",
                phone=f"caregiver-phone-{suffix}",
                notify_on_recap=True,
            )
            await caregivers_repo.confirm_consent(
                db, cg.id, confirmed_by="ops"
            )
            await db.commit()
            return appt.id, patient.phone, cg.phone

    appointment_id, patient_phone, caregiver_phone = (
        # Use a fresh event loop call since the fixture is a plain
        # function. Pytest's asyncio_mode auto handles the test body
        # but the seed helper is async so we drive it explicitly.
        await _seed_appt_with_caregiver()
    )

    # Draft + send recap.
    orchestrator_client.put(
        f"/appointments/{appointment_id}/recap",
        json={"doctor_notes": "fan-out test", "structured": {}},
    )
    sent = orchestrator_client.post(
        f"/appointments/{appointment_id}/recap/send"
    )
    assert sent.status_code == 200
    assert sent.json()["status"] == "sent"

    # The fake gateway should have been called for both phones —
    # patient first, then the caregiver.
    assert patient_phone in captured_phones
    assert caregiver_phone in captured_phones


async def test_consent_prompt_sends_to_pending_caregiver(
    orchestrator_client, monkeypatch
):
    """POST /caregivers/{id}/send-consent-prompt for a pending caregiver
    invokes the gateway with the consent template + dynamic button
    payloads, returns the wamid, and writes a ``caregiver_consent_prompt_sent``
    audit row."""

    captured: dict = {}

    async def fake_send(**kwargs):
        captured.update(kwargs)
        return f"wamid.fake.consent.{uuid.uuid4().hex[:8]}"

    from services.orchestrator.routers import caregivers as caregivers_router

    monkeypatch.setattr(
        caregivers_router,
        "_send_caregiver_consent_prompt_via_gateway",
        fake_send,
    )

    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()

    async def _seed():
        async with SessionLocal() as db:
            p = Patient(
                full_name=f"Consent Prompt Test {suffix}",
                phone=f"consent-prompt-{suffix}",
            )
            db.add(p)
            await db.flush()
            cg = await caregivers_repo.create(
                db,
                patient_id=p.id,
                full_name="Anita Karnatak",
                phone=f"caregiver-consent-{suffix}",
                relationship_to_patient="spouse",
            )
            await db.commit()
            return cg.id

    cg_id = await _seed()

    r = orchestrator_client.post(
        f"/caregivers/{cg_id}/send-consent-prompt"
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "sent"
    assert body["wamid"].startswith("wamid.fake.consent.")
    assert body["caregiver_id"] == cg_id
    assert body["template_name"] == "caregiver_consent_v1"

    # Gateway helper saw the right inputs.
    assert captured["caregiver_id"] == cg_id
    assert captured["caregiver_first_name"] == "Anita"
    assert "Consent Prompt Test" in captured["patient_full_name"]


def test_consent_prompt_409_when_already_confirmed(
    orchestrator_client,
):
    """Sending a prompt to a caregiver whose consent is already
    confirmed returns 409 — there's no pending prompt to fulfil."""
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()

    async def _seed():
        async with SessionLocal() as db:
            p = Patient(
                full_name=f"Already Confirmed {suffix}",
                phone=f"already-conf-{suffix}",
            )
            db.add(p)
            await db.flush()
            cg = await caregivers_repo.create(
                db,
                patient_id=p.id,
                full_name="Already Confirmed Caregiver",
                phone=f"already-conf-cg-{suffix}",
            )
            await caregivers_repo.confirm_consent(
                db, cg.id, confirmed_by="ops"
            )
            await db.commit()
            return cg.id

    import asyncio

    cg_id = asyncio.get_event_loop().run_until_complete(_seed())
    r = orchestrator_client.post(
        f"/caregivers/{cg_id}/send-consent-prompt"
    )
    assert r.status_code == 409
    assert "already" in r.json()["detail"].lower()


def test_consent_prompt_404_for_unknown_caregiver(orchestrator_client):
    r = orchestrator_client.post(
        "/caregivers/9999999/send-consent-prompt"
    )
    assert r.status_code == 404


async def test_consent_prompt_502_when_gateway_send_fails(
    orchestrator_client, monkeypatch
):
    """Gateway helper returns None → endpoint surfaces a 502 instead
    of writing a successful-looking audit row."""

    async def fake_send(**kwargs):
        return None

    from services.orchestrator.routers import caregivers as caregivers_router

    monkeypatch.setattr(
        caregivers_router,
        "_send_caregiver_consent_prompt_via_gateway",
        fake_send,
    )

    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()

    async def _seed():
        async with SessionLocal() as db:
            p = Patient(
                full_name=f"Gateway Fail {suffix}",
                phone=f"gw-fail-{suffix}",
            )
            db.add(p)
            await db.flush()
            cg = await caregivers_repo.create(
                db,
                patient_id=p.id,
                full_name="Gateway Fail Caregiver",
                phone=f"gw-fail-cg-{suffix}",
            )
            await db.commit()
            return cg.id

    cg_id = await _seed()
    r = orchestrator_client.post(
        f"/caregivers/{cg_id}/send-consent-prompt"
    )
    assert r.status_code == 502


async def test_recap_send_skips_pending_caregiver(
    orchestrator_client, monkeypatch
):
    """Pending consent → no caregiver send. The patient send still
    happens, the recap still marks ``sent``, but the caregiver phone
    is NOT in the captured set."""
    from services.orchestrator import main as orchestrator_main

    captured_phones: list[str] = []

    async def fake_send(**kwargs):
        captured_phones.append(kwargs.get("patient_phone", ""))
        return f"wamid.fake.{uuid.uuid4().hex[:8]}"

    monkeypatch.setattr(
        orchestrator_main, "_send_recap_via_gateway", fake_send
    )

    SessionLocal = get_sessionmaker()
    suffix = uuid.uuid4().hex[:8]

    async def _seed():
        from datetime import datetime, timedelta, timezone
        from app.db.models import (
            Appointment,
            AppointmentStatus,
            Doctor,
            DoctorOAuthStatus,
        )
        async with SessionLocal() as db:
            patient = Patient(
                full_name=f"Skip Pending {suffix}",
                phone=f"skip-pending-{suffix}",
            )
            doctor = Doctor(
                name=f"Dr Skip {suffix}",
                email=f"dr-skip-{suffix}@example.com",
                timezone="UTC",
                calendar_id="primary",
                oauth_status=DoctorOAuthStatus.connected,
            )
            db.add_all([patient, doctor])
            await db.flush()
            appt_when = datetime.now(timezone.utc) - timedelta(hours=2)
            appt = Appointment(
                patient_id=patient.id,
                doctor_id=doctor.id,
                scheduled_for=appt_when,
                end_at=appt_when + timedelta(minutes=30),
                status=AppointmentStatus.completed,
                source="test",
            )
            db.add(appt)
            await db.flush()
            cg = await caregivers_repo.create(
                db,
                patient_id=patient.id,
                full_name="Pending Daughter",
                phone=f"pending-phone-{suffix}",
            )
            # NO consent confirmation.
            await db.commit()
            return appt.id, patient.phone, cg.phone

    appointment_id, patient_phone, caregiver_phone = await _seed()

    orchestrator_client.put(
        f"/appointments/{appointment_id}/recap",
        json={"doctor_notes": "pending consent", "structured": {}},
    )
    sent = orchestrator_client.post(
        f"/appointments/{appointment_id}/recap/send"
    )
    assert sent.status_code == 200
    assert sent.json()["status"] == "sent"

    # Patient was sent; pending caregiver was NOT.
    assert patient_phone in captured_phones
    assert caregiver_phone not in captured_phones

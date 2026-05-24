"""Integration tests for the DSAR patient-export endpoint.

End-to-end against a real Postgres because the export is the
ultimate aggregation surface (touches ~10 tables) and we want to
confirm both the SQL dance AND the audit-log write actually happen.

Skipped when DATABASE_URL is unset.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import (
    AdherenceEvent,
    AdherenceStatus,
    AuditRecord,
    Patient,
    Regimen,
)
from app.db.repositories import (
    ops_tickets as ops_tickets_repo,
    patients as patients_repo,
)
from app.db.session import get_sessionmaker

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping patient export integration tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


async def _create_patient_with_data() -> tuple[int, str]:
    """Seed a patient with a regimen + adherence event +
    side-effect ticket so the export has things to walk."""
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(
            full_name=f"Export Test {suffix}",
            phone=f"export-test-{suffix}",
            consent_sms=True,
            preferred_language="hi",
        )
        db.add(p)
        await db.flush()

        regimen = Regimen(
            patient_id=p.id,
            medication_name="Metformin",
            dose="500 mg",
            schedule={"type": "times_of_day", "times": ["08:00"]},
        )
        db.add(regimen)
        await db.flush()

        adherence = AdherenceEvent(
            patient_id=p.id,
            regimen_id=regimen.id,
            scheduled_at=datetime.now(timezone.utc) - timedelta(days=1),
            status=AdherenceStatus.taken,
            confirmed_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
        db.add(adherence)
        await db.flush()
        await db.commit()

        # Side-effect report — exercises the notes-extraction path.
        await ops_tickets_repo.create(
            db,
            patient_id=p.phone,
            category="side_effect_report",
            priority="high",
            sla_minutes=30,
            notes="Patient said:\n  > metformin gave me headaches",
        )
        await db.commit()
        return p.id, p.phone


# ---- Endpoint round-trip --------------------------------------------------


def test_export_returns_full_document(orchestrator_client):
    """End-to-end: GET /patients/{id}/export returns a JSON
    document with every documented section + the counts block.
    Section presence is what the next-of-kin / regulator would
    check first; concrete shape is asserted on the seeded rows."""
    import asyncio

    pid, _phone = asyncio.get_event_loop().run_until_complete(
        _create_patient_with_data()
    )

    r = orchestrator_client.get(
        f"/patients/{pid}/export", params={"actor": "test_runner"}
    )
    assert r.status_code == 200
    body = r.json()

    # Top-level metadata.
    assert body["schema_version"] == 1
    assert body["exported_by"] == "test_runner"
    assert body["window_days"] == 365
    assert body["exported_at"]  # ISO string present

    # Patient section.
    patient = body["patient"]
    assert patient["id"] == pid
    assert patient["preferred_language"] == "hi"
    assert "consents" in patient
    assert patient["consents"]["sms"] is True
    assert "cohorts" in patient
    assert "bot_paused" in patient

    # All documented sections present (empty lists are OK; missing
    # keys are not — the schema is the contract).
    for section in (
        "caregivers",
        "regimens",
        "appointments",
        "adherence_events",
        "lab_followups",
        "appointment_recaps",
        "cohort_tags",
        "care_plan_exemptions",
        "side_effect_reports",
    ):
        assert section in body, f"missing section: {section}"

    # Seeded data shows up.
    assert body["counts"]["regimens"] == 1
    assert body["counts"]["adherence_events"] == 1
    assert body["counts"]["side_effect_reports"] == 1

    # Verbatim side-effect text extracted from notes.
    se = body["side_effect_reports"][0]
    assert se["reported_text"] == "metformin gave me headaches"


def test_export_writes_audit_record(orchestrator_client):
    """Every successful export must write an AuditRecord — that's
    the regulator-trace evidence. Without this, "who exported
    this patient's data?" has no answer."""
    import asyncio

    loop = asyncio.get_event_loop()
    pid, phone = loop.run_until_complete(_create_patient_with_data())

    r = orchestrator_client.get(
        f"/patients/{pid}/export", params={"actor": "ops_alice"}
    )
    assert r.status_code == 200

    # Read the audit log directly — the endpoint commits before
    # returning so the row should be visible.
    async def _read_audit():
        SessionLocal = get_sessionmaker()
        async with SessionLocal() as db:
            stmt = (
                select(AuditRecord)
                .where(AuditRecord.patient_id == phone)
                .where(AuditRecord.record_type == "patient_data_export")
            )
            return list(
                (await db.execute(stmt)).scalars().all()
            )

    rows = loop.run_until_complete(_read_audit())
    assert len(rows) >= 1
    audit = rows[-1]
    assert "dsar_right_of_access" in (audit.reason_codes or [])
    assert audit.details.get("actor") == "ops_alice"


def test_export_prefers_x_ops_actor_header(orchestrator_client):
    """The X-Ops-Actor header (set by the ops console from the operator
    session) takes precedence over the legacy ?actor= query param so the
    operator identity stays out of the URL / access logs."""
    import asyncio

    pid, _phone = asyncio.get_event_loop().run_until_complete(
        _create_patient_with_data()
    )

    r = orchestrator_client.get(
        f"/patients/{pid}/export",
        params={"actor": "from_query"},
        headers={"X-Ops-Actor": "from_header"},
    )
    assert r.status_code == 200
    assert r.json()["exported_by"] == "from_header"


def test_export_404_for_unknown_patient(orchestrator_client):
    r = orchestrator_client.get(
        "/patients/999999999/export", params={"actor": "ops"}
    )
    assert r.status_code == 404


def test_export_validates_window_days(orchestrator_client):
    """Out-of-range window_days must 400, not silently truncate.
    A ``window_days=0`` request would produce an empty document
    that looks like a real export — bad."""
    import asyncio

    pid, _ = asyncio.get_event_loop().run_until_complete(
        _create_patient_with_data()
    )

    r = orchestrator_client.get(
        f"/patients/{pid}/export",
        params={"actor": "ops", "window_days": 0},
    )
    assert r.status_code == 400

    r = orchestrator_client.get(
        f"/patients/{pid}/export",
        params={"actor": "ops", "window_days": 99999},
    )
    assert r.status_code == 400


def test_export_window_days_filters_old_data(orchestrator_client):
    """A short ``window_days`` should drop adherence/appointment/
    recap rows older than the window. Confirms the bounded
    sections honour the parameter rather than always returning
    everything."""
    import asyncio

    loop = asyncio.get_event_loop()
    pid, _ = loop.run_until_complete(_create_patient_with_data())

    SessionLocal = get_sessionmaker()

    # Backdate the seeded adherence event well outside any
    # reasonable window so a 1-day window excludes it.
    async def _backdate():
        async with SessionLocal() as db:
            stmt = select(AdherenceEvent).where(
                AdherenceEvent.patient_id == pid
            )
            rows = list((await db.execute(stmt)).scalars().all())
            for row in rows:
                row.scheduled_at = datetime.now(
                    timezone.utc
                ) - timedelta(days=400)
            await db.commit()

    loop.run_until_complete(_backdate())

    # 1-day window: adherence row from 400 days ago must be filtered.
    r = orchestrator_client.get(
        f"/patients/{pid}/export",
        params={"actor": "ops", "window_days": 1},
    )
    assert r.status_code == 200
    assert r.json()["counts"]["adherence_events"] == 0

    # 500-day window: the same row reappears.
    r = orchestrator_client.get(
        f"/patients/{pid}/export",
        params={"actor": "ops", "window_days": 500},
    )
    assert r.status_code == 200
    assert r.json()["counts"]["adherence_events"] >= 1


def test_export_includes_pause_state_on_paused_patient(orchestrator_client):
    """A patient currently paused by ops must show the pause
    metadata in the export (DSAR is "data we hold, including
    operational state")."""
    import asyncio

    loop = asyncio.get_event_loop()
    pid, _ = loop.run_until_complete(_create_patient_with_data())

    SessionLocal = get_sessionmaker()

    async def _pause():
        async with SessionLocal() as db:
            await patients_repo.pause_bot(
                db, pid, actor="ops_test", reason="export integration"
            )
            await db.commit()

    loop.run_until_complete(_pause())

    r = orchestrator_client.get(
        f"/patients/{pid}/export", params={"actor": "ops"}
    )
    assert r.status_code == 200
    paused = r.json()["patient"]["bot_paused"]
    assert paused["at"] is not None
    assert paused["reason"] == "export integration"
    assert paused["by"] == "ops_test"

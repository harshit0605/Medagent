"""Integration tests for the side-effect history surface on the
patient detail endpoint.

End-to-end: open a ``side_effect_report`` ticket via the
``side_effect_handler`` for a real patient, then GET the patient
detail and confirm the ticket surfaces in
``recent_side_effect_reports`` with the verbatim reported_text
extracted from the notes.

Skipped when DATABASE_URL is unset.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.db.models import Patient
from app.db.repositories import ops_tickets as ops_tickets_repo
from app.db.session import get_sessionmaker
from services.orchestrator.side_effect_handler import (
    handle_side_effect_report,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping side-effect history tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


async def _create_patient() -> tuple[int, str]:
    """Create a fresh patient and return (id, phone). Phone has the
    test prefix so cleanup is identifiable."""
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(
            full_name=f"SE History Test {suffix}",
            phone=f"se-history-{suffix}",
            consent_sms=True,
        )
        db.add(p)
        await db.flush()
        await db.commit()
        return p.id, p.phone


# ---- Repo helper ---------------------------------------------------------


async def test_list_for_patient_by_category_filters_by_category():
    """The new ``list_for_patient_by_category`` returns ONLY the
    tickets matching both patient_id AND category. A patient with
    a side_effect_report AND a refill_help ticket should see only
    the side_effect_report when querying for that category."""
    pid, phone = await _create_patient()
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        # Two tickets: one side_effect_report, one refill_help.
        await ops_tickets_repo.create(
            db,
            patient_id=phone,
            category="side_effect_report",
            priority="high",
            sla_minutes=30,
            notes="Patient said:\n  > nausea from the meds",
        )
        await ops_tickets_repo.create(
            db,
            patient_id=phone,
            category="refill_help",
            priority="p2",
            sla_minutes=240,
            notes="needs help with refill",
        )
        await db.commit()

        rows = await ops_tickets_repo.list_for_patient_by_category(
            db, phone, "side_effect_report"
        )
        assert len(rows) == 1
        assert rows[0].category == "side_effect_report"

        # Cross-check: querying the other category returns the other.
        rows_refill = (
            await ops_tickets_repo.list_for_patient_by_category(
                db, phone, "refill_help"
            )
        )
        assert len(rows_refill) == 1
        assert rows_refill[0].category == "refill_help"


# ---- End-to-end through the handler ---------------------------------------


def test_patient_detail_surfaces_recent_side_effect_reports(
    orchestrator_client,
):
    """End-to-end: handler opens a side_effect_report ticket →
    GET /patients/{id} surfaces it in recent_side_effect_reports
    with the verbatim ``Patient said:`` block extracted into
    ``reported_text``."""
    import asyncio

    loop = asyncio.get_event_loop()
    pid, phone = loop.run_until_complete(_create_patient())

    # Drive the handler. It loads the patient by phone, opens the
    # ticket with the standard notes format the extractor parses.
    inbound = "I started getting bad headaches from the new metformin"
    delta = loop.run_until_complete(
        handle_side_effect_report(
            patient_phone=phone, new_user_text=inbound
        )
    )
    assert delta is not None
    assert delta["audit_reasons"] == ["side_effect_logged"]

    # GET the detail endpoint — the ticket should be on the patient.
    r = orchestrator_client.get(f"/patients/{pid}")
    assert r.status_code == 200
    body = r.json()
    assert "recent_side_effect_reports" in body
    reports = body["recent_side_effect_reports"]
    assert len(reports) == 1
    report = reports[0]
    # Status fields come from the ticket directly.
    assert report["status"] == "open"
    assert report["priority"] == "high"
    # Verbatim inbound surfaced via the extractor.
    assert (
        report["reported_text"]
        == "I started getting bad headaches from the new metformin"
    )


def test_patient_detail_returns_empty_list_when_no_reports(
    orchestrator_client,
):
    """A patient who has never reported a side effect must see an
    empty list (not null, not missing key) — the UI checks
    ``length > 0`` to decide whether to render the section."""
    import asyncio

    pid, _phone = asyncio.get_event_loop().run_until_complete(
        _create_patient()
    )
    r = orchestrator_client.get(f"/patients/{pid}")
    assert r.status_code == 200
    body = r.json()
    assert body.get("recent_side_effect_reports") == []


def test_patient_detail_orders_reports_newest_first(orchestrator_client):
    """Multiple reports → newest first. A doctor scanning the
    timeline cares about the most recent event most; older
    history is contextual."""
    import asyncio
    import time

    loop = asyncio.get_event_loop()
    pid, phone = loop.run_until_complete(_create_patient())

    SessionLocal = get_sessionmaker()
    # Two tickets — different reported_text.
    async def _seed():
        async with SessionLocal() as db:
            await ops_tickets_repo.create(
                db,
                patient_id=phone,
                category="side_effect_report",
                priority="high",
                sla_minutes=30,
                notes="Patient said:\n  > first report",
            )
            await db.commit()
        # Tiny sleep so created_at ordering is deterministic at
        # second-precision (Postgres timestamp precision sometimes
        # equates events created in the same ms).
        time.sleep(1.1)
        async with SessionLocal() as db:
            await ops_tickets_repo.create(
                db,
                patient_id=phone,
                category="side_effect_report",
                priority="high",
                sla_minutes=30,
                notes="Patient said:\n  > second report",
            )
            await db.commit()

    loop.run_until_complete(_seed())

    r = orchestrator_client.get(f"/patients/{pid}")
    assert r.status_code == 200
    reports = r.json()["recent_side_effect_reports"]
    assert len(reports) == 2
    # Newest first.
    assert reports[0]["reported_text"] == "second report"
    assert reports[1]["reported_text"] == "first report"

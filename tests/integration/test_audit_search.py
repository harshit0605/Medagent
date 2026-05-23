"""Integration tests for the audit log search repo helper +
GET /ops/audit-search endpoint.

End-to-end against real Postgres because:
    1. The JSON-array containment for ``reason_code`` filter uses
       SQLAlchemy's ``contains`` lowering — only meaningful against
       a real database.
    2. Date-range filters need real timestamp comparisons across
       the rolling window.
    3. The pagination + total count are what the UI relies on for
       "showing 1-50 of 312" — confirms the count query mirrors
       the rows query exactly.

Skipped when DATABASE_URL is unset.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.db.repositories import audit as audit_repo
from app.db.session import get_sessionmaker

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping audit-search integration tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


def _phone() -> str:
    """Per-test unique synthetic phone — the integration suite has
    no per-test isolation, so reusing a phone would conflate audit
    rows across tests."""
    return f"audit-test-{uuid.uuid4().hex[:10]}"


async def _seed_audit_rows(*, phone: str, count: int, **defaults):
    """Drop ``count`` audit rows for ``phone`` with the supplied
    defaults. Returns the list of created rows."""
    SessionLocal = get_sessionmaker()
    rows = []
    async with SessionLocal() as db:
        for _ in range(count):
            row = await audit_repo.log_workflow_summary(
                db,
                patient_id=phone,
                outbound_mode=defaults.get("outbound_mode", "FREEFORM"),
                flow_action=defaults.get("flow_action", "ALLOW"),
                reason_codes=defaults.get("reason_codes", []),
                details=defaults.get("details", {}),
            )
            rows.append(row)
        await db.commit()
    return rows


# ---- Repo-level filter coverage ------------------------------------------


async def test_search_filters_by_patient_id():
    phone_a = _phone()
    phone_b = _phone()
    await _seed_audit_rows(phone=phone_a, count=3)
    await _seed_audit_rows(phone=phone_b, count=2)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows_a, total_a = await audit_repo.search(
            db, patient_id=phone_a
        )
        rows_b, total_b = await audit_repo.search(
            db, patient_id=phone_b
        )
        assert total_a == 3
        assert total_b == 2
        assert all(r.patient_id == phone_a for r in rows_a)
        assert all(r.patient_id == phone_b for r in rows_b)


async def test_search_filters_by_record_type():
    """``record_type`` is the strongest discriminator. A
    patient with both ``workflow_summary`` and
    ``patient_data_export`` rows must be filterable to one
    type at a time."""
    phone = _phone()
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        # 2 workflow_summary rows.
        for _ in range(2):
            await audit_repo.log_workflow_summary(
                db,
                patient_id=phone,
                outbound_mode="FREEFORM",
                flow_action="ALLOW",
                reason_codes=[],
            )
        # 1 patient_data_export row.
        await audit_repo.log_patient_data_export(
            db,
            patient_id=phone,
            actor="ops",
            reason_codes=["dsar_right_of_access"],
        )
        await db.commit()

        rows, total = await audit_repo.search(
            db,
            patient_id=phone,
            record_type="patient_data_export",
        )
        assert total == 1
        assert rows[0].record_type == "patient_data_export"

        rows, total = await audit_repo.search(
            db,
            patient_id=phone,
            record_type="workflow_summary",
        )
        assert total == 2


async def test_search_filters_by_reason_code():
    """The JSON array containment lowering is the most fragile
    filter — a real DB query confirms ``reason_codes contains
    ['x']`` actually matches rows whose JSON array includes
    ``'x'``."""
    phone = _phone()
    await _seed_audit_rows(
        phone=phone, count=2, reason_codes=["rate_limited"]
    )
    await _seed_audit_rows(
        phone=phone, count=3, reason_codes=["other_reason"]
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows, total = await audit_repo.search(
            db, patient_id=phone, reason_code="rate_limited"
        )
        assert total == 2
        for row in rows:
            assert "rate_limited" in (row.reason_codes or [])


async def test_search_filters_by_flow_action():
    phone = _phone()
    await _seed_audit_rows(phone=phone, count=2, flow_action="HOLD")
    await _seed_audit_rows(phone=phone, count=3, flow_action="ALLOW")

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows, total = await audit_repo.search(
            db, patient_id=phone, flow_action="HOLD"
        )
        assert total == 2
        for row in rows:
            assert row.flow_action == "HOLD"


async def test_search_filters_by_date_range():
    """Date filters bound the rolling window — old rows must not
    leak in when the operator filters to the last hour."""
    phone = _phone()
    # Backdate one row to 2 days ago, leave another at "now".
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        old = await audit_repo.log_workflow_summary(
            db,
            patient_id=phone,
            outbound_mode="FREEFORM",
            flow_action="ALLOW",
            reason_codes=[],
            logged_at=datetime.now(timezone.utc)
            - timedelta(days=2),
        )
        new = await audit_repo.log_workflow_summary(
            db,
            patient_id=phone,
            outbound_mode="FREEFORM",
            flow_action="ALLOW",
            reason_codes=[],
        )
        await db.commit()

        # Last hour → only the recent row.
        rows, total = await audit_repo.search(
            db,
            patient_id=phone,
            since=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        assert total == 1
        assert rows[0].id == new.id

        # Last 3 days → both rows.
        rows, total = await audit_repo.search(
            db,
            patient_id=phone,
            since=datetime.now(timezone.utc) - timedelta(days=3),
        )
        assert total == 2

        # Until 1 day ago → only the old row.
        rows, total = await audit_repo.search(
            db,
            patient_id=phone,
            until=datetime.now(timezone.utc) - timedelta(days=1),
        )
        assert total == 1
        assert rows[0].id == old.id


async def test_search_returns_total_count_unaffected_by_pagination():
    """``total`` reflects the unpaginated count so the UI can
    render "showing 1-50 of 312" — limiting the rows must NOT
    cap the count."""
    phone = _phone()
    await _seed_audit_rows(phone=phone, count=15)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows, total = await audit_repo.search(
            db, patient_id=phone, limit=5
        )
        assert len(rows) == 5
        assert total == 15

        # Page 2 returns the next 5 rows; total stays the same.
        rows_p2, total_p2 = await audit_repo.search(
            db, patient_id=phone, limit=5, offset=5
        )
        assert len(rows_p2) == 5
        assert total_p2 == 15
        # Disjoint pages.
        assert {r.id for r in rows} & {r.id for r in rows_p2} == set()


async def test_search_orders_newest_first():
    phone = _phone()
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        # Three rows with explicit logged_at timestamps so the
        # ordering is deterministic at sub-second precision.
        first = await audit_repo.log_workflow_summary(
            db,
            patient_id=phone,
            outbound_mode="FREEFORM",
            flow_action="ALLOW",
            reason_codes=[],
            logged_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        second = await audit_repo.log_workflow_summary(
            db,
            patient_id=phone,
            outbound_mode="FREEFORM",
            flow_action="ALLOW",
            reason_codes=[],
            logged_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        third = await audit_repo.log_workflow_summary(
            db,
            patient_id=phone,
            outbound_mode="FREEFORM",
            flow_action="ALLOW",
            reason_codes=[],
        )
        await db.commit()

        rows, _total = await audit_repo.search(db, patient_id=phone)
        assert [r.id for r in rows] == [third.id, second.id, first.id]


# ---- Endpoint round-trip --------------------------------------------------


def test_endpoint_returns_filtered_rows(orchestrator_client):
    """Full /ops/audit-search round-trip with filters applied."""
    import asyncio

    phone = _phone()
    asyncio.get_event_loop().run_until_complete(
        _seed_audit_rows(
            phone=phone, count=3, reason_codes=["rate_limited"]
        )
    )

    response = orchestrator_client.get(
        "/ops/audit-search",
        params={
            "patient_id": phone,
            "reason_code": "rate_limited",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert len(body["rows"]) == 3
    for row in body["rows"]:
        assert row["patient_id"] == phone
        assert "rate_limited" in row["reason_codes"]


def test_endpoint_validates_limit(orchestrator_client):
    response = orchestrator_client.get(
        "/ops/audit-search",
        params={"patient_id": "x", "limit": 0},
    )
    assert response.status_code == 400

    response = orchestrator_client.get(
        "/ops/audit-search",
        params={"patient_id": "x", "limit": 500},
    )
    assert response.status_code == 400


def test_endpoint_rejects_bad_datetime(orchestrator_client):
    """Bad ISO datetimes should 400 rather than silently dropping
    the filter — operators need to see what went wrong."""
    response = orchestrator_client.get(
        "/ops/audit-search",
        params={"patient_id": "x", "since": "not-a-date"},
    )
    assert response.status_code == 400


def test_endpoint_accepts_date_only(orchestrator_client):
    """The UI uses ``<input type="date">`` which sends YYYY-MM-DD.
    Date-only must parse without error (becomes midnight UTC)."""
    response = orchestrator_client.get(
        "/ops/audit-search",
        params={"patient_id": "non-existent", "since": "2026-05-08"},
    )
    assert response.status_code == 200

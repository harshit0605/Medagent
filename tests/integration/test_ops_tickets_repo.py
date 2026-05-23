"""Integration tests for the ops_tickets repo extension (assign / snooze /
unsnooze / reopen / append_note / list_with_filters).

Skipped when DATABASE_URL is unset so CI without a Postgres can still pass.
Each test uses a unique synthetic patient_id so reruns don't collide.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.db.models import OpsTicketStatus
from app.db.repositories import ops_tickets as ops_tickets_repo
from app.db.session import get_sessionmaker

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping ops_tickets integration tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


@pytest.fixture()
def patient_id() -> str:
    return f"itest-ticket-{uuid.uuid4().hex[:10]}"


async def _create(patient: str, *, category: str = "triage") -> int:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        ticket = await ops_tickets_repo.create(
            db,
            patient_id=patient,
            category=category,
            priority="p1",
            sla_minutes=60,
            notes="initial",
        )
        await db.commit()
        return ticket.id


async def _refetch(ticket_id: int):
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        return await ops_tickets_repo.get(db, ticket_id)


async def test_assign_sets_assigned_to_and_appends_note(patient_id):
    ticket_id = await _create(patient_id)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        updated = await ops_tickets_repo.assign(
            db, ticket_id, assigned_to="alice", actor="bob"
        )
        await db.commit()
        assert updated is not None
        assert updated.assigned_to == "alice"
        assert "bob: assigned to alice" in (updated.notes or "")
        assert "initial" in (updated.notes or "")  # original preserved


async def test_assign_with_empty_string_unassigns(patient_id):
    ticket_id = await _create(patient_id)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await ops_tickets_repo.assign(
            db, ticket_id, assigned_to="alice", actor="bob"
        )
        await db.commit()
        cleared = await ops_tickets_repo.assign(
            db, ticket_id, assigned_to=None, actor="bob"
        )
        await db.commit()
        assert cleared is not None
        assert cleared.assigned_to is None
        assert "bob: unassigned" in (cleared.notes or "")


async def test_snooze_sets_future_snoozed_until(patient_id):
    ticket_id = await _create(patient_id)
    until = datetime.now(timezone.utc) + timedelta(hours=2)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        updated = await ops_tickets_repo.snooze(
            db, ticket_id, until=until, actor="alice"
        )
        await db.commit()
        assert updated is not None
        assert updated.snoozed_until is not None
        # Status MUST stay unchanged — snooze is a hide-from-queue signal.
        assert updated.status == OpsTicketStatus.open
        assert "alice: snoozed until" in (updated.notes or "")


async def test_unsnooze_clears_snoozed_until(patient_id):
    ticket_id = await _create(patient_id)
    until = datetime.now(timezone.utc) + timedelta(hours=1)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await ops_tickets_repo.snooze(db, ticket_id, until=until, actor="x")
        await db.commit()
        cleared = await ops_tickets_repo.unsnooze(db, ticket_id, actor="y")
        await db.commit()
        assert cleared is not None
        assert cleared.snoozed_until is None
        assert "y: snooze cleared" in (cleared.notes or "")


async def test_reopen_clears_resolved_and_acknowledged(patient_id):
    ticket_id = await _create(patient_id)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await ops_tickets_repo.acknowledge(db, ticket_id, actor="ops")
        await ops_tickets_repo.resolve(db, ticket_id, actor="ops", notes="closed")
        await db.commit()
        reopened = await ops_tickets_repo.reopen(
            db, ticket_id, actor="ops", note="back to triage"
        )
        await db.commit()
        assert reopened is not None
        assert reopened.status == OpsTicketStatus.open
        assert reopened.resolved_at is None
        assert reopened.acknowledged_at is None
        assert "ops: reopened — back to triage" in (reopened.notes or "")


async def test_append_note_prepends_chronologically(patient_id):
    ticket_id = await _create(patient_id)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        first = await ops_tickets_repo.append_note(
            db, ticket_id, actor="alice", note="called patient"
        )
        await db.commit()
        second = await ops_tickets_repo.append_note(
            db, ticket_id, actor="bob", note="left voicemail"
        )
        await db.commit()
        assert first is not None and second is not None
        notes = second.notes or ""
        # Second note should appear before the first (newest first), and the
        # original "initial" body should still be present at the bottom.
        idx_second = notes.find("bob: left voicemail")
        idx_first = notes.find("alice: called patient")
        idx_initial = notes.find("initial")
        assert 0 <= idx_second < idx_first < idx_initial


async def test_list_with_filters_only_active_excludes_snoozed(patient_id):
    ticket_id = await _create(patient_id)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await ops_tickets_repo.snooze(
            db,
            ticket_id,
            until=datetime.now(timezone.utc) + timedelta(hours=1),
            actor="ops",
        )
        await db.commit()

        active = await ops_tickets_repo.list_with_filters(db, only_active=True)
        snoozed = await ops_tickets_repo.list_with_filters(db, only_snoozed=True)

        active_ids = {t.id for t in active}
        snoozed_ids = {t.id for t in snoozed}
        assert ticket_id not in active_ids
        assert ticket_id in snoozed_ids


async def test_list_with_filters_status_and_category_combine(patient_id):
    other = await _create(patient_id, category="lab_help")
    triage = await _create(patient_id, category="triage")

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows = await ops_tickets_repo.list_with_filters(
            db, status="open", category="triage"
        )
        ids = {t.id for t in rows}
        assert triage in ids
        assert other not in ids


# ---- HTTP endpoint smoke -------------------------------------------------


def test_assign_endpoint_round_trip(orchestrator_client, patient_id):
    create = orchestrator_client.post(
        "/ops/tickets",
        json={
            "patient_id": patient_id,
            "category": "triage",
            "priority": "p1",
            "sla_minutes": 30,
        },
    )
    assert create.status_code == 200
    ticket_id = create.json()["ticket_id"]

    assign = orchestrator_client.post(
        f"/ops/tickets/{ticket_id}/assign",
        json={"actor": "carol", "assigned_to": "dana"},
    )
    assert assign.status_code == 200
    body = assign.json()
    assert body["assigned_to"] == "dana"
    assert "carol: assigned to dana" in (body["notes"] or "")


def test_snooze_unsnooze_endpoint_round_trip(orchestrator_client, patient_id):
    create = orchestrator_client.post(
        "/ops/tickets",
        json={
            "patient_id": patient_id,
            "category": "triage",
            "priority": "p1",
            "sla_minutes": 30,
        },
    )
    ticket_id = create.json()["ticket_id"]

    snooze = orchestrator_client.post(
        f"/ops/tickets/{ticket_id}/snooze",
        json={"actor": "ops", "minutes": 60, "notes": "low priority"},
    )
    assert snooze.status_code == 200
    body = snooze.json()
    assert body["snoozed_until"] is not None
    assert body["is_snoozed"] is True
    assert body["status"] == "open"

    # Missing both `minutes` and `until` should 400.
    bad = orchestrator_client.post(
        f"/ops/tickets/{ticket_id}/snooze", json={"actor": "ops"}
    )
    assert bad.status_code == 400

    unsnooze = orchestrator_client.post(
        f"/ops/tickets/{ticket_id}/unsnooze", json={"actor": "ops"}
    )
    assert unsnooze.status_code == 200
    assert unsnooze.json()["snoozed_until"] is None
    assert unsnooze.json()["is_snoozed"] is False


def test_reopen_endpoint_round_trip(orchestrator_client, patient_id):
    create = orchestrator_client.post(
        "/ops/tickets",
        json={
            "patient_id": patient_id,
            "category": "triage",
            "priority": "p1",
            "sla_minutes": 30,
        },
    )
    ticket_id = create.json()["ticket_id"]

    orchestrator_client.post(
        f"/ops/tickets/{ticket_id}/resolve",
        json={"actor": "ops", "notes": "fixed"},
    )

    reopen = orchestrator_client.post(
        f"/ops/tickets/{ticket_id}/reopen",
        json={"actor": "ops", "notes": "regression"},
    )
    assert reopen.status_code == 200
    body = reopen.json()
    assert body["status"] == "open"
    assert body["resolved_at"] is None


def test_note_endpoint_appends(orchestrator_client, patient_id):
    create = orchestrator_client.post(
        "/ops/tickets",
        json={
            "patient_id": patient_id,
            "category": "triage",
            "priority": "p1",
            "sla_minutes": 30,
        },
    )
    ticket_id = create.json()["ticket_id"]

    note = orchestrator_client.post(
        f"/ops/tickets/{ticket_id}/note",
        json={"actor": "alice", "note": "called family"},
    )
    assert note.status_code == 200
    assert "alice: called family" in (note.json()["notes"] or "")

    # Empty note should be rejected by validation.
    rejected = orchestrator_client.post(
        f"/ops/tickets/{ticket_id}/note",
        json={"actor": "alice", "note": ""},
    )
    assert rejected.status_code == 422


def test_list_view_active_filter(orchestrator_client, patient_id):
    create = orchestrator_client.post(
        "/ops/tickets",
        json={
            "patient_id": patient_id,
            "category": "triage",
            "priority": "p1",
            "sla_minutes": 30,
        },
    )
    ticket_id = create.json()["ticket_id"]

    # snooze it for an hour
    orchestrator_client.post(
        f"/ops/tickets/{ticket_id}/snooze",
        json={"actor": "ops", "minutes": 60},
    )

    active = orchestrator_client.get(
        "/ops/tickets", params={"view": "active"}
    ).json()
    snoozed = orchestrator_client.get(
        "/ops/tickets", params={"view": "snoozed"}
    ).json()

    active_ids = {t["ticket_id"] for t in active}
    snoozed_ids = {t["ticket_id"] for t in snoozed}
    assert ticket_id not in active_ids
    assert ticket_id in snoozed_ids


def test_get_ticket_endpoint_returns_404_for_unknown(orchestrator_client):
    response = orchestrator_client.get("/ops/tickets/999999999")
    assert response.status_code == 404


def test_dashboard_includes_tickets_sla_overdue(orchestrator_client):
    response = orchestrator_client.get("/ops/dashboard")
    assert response.status_code == 200
    alerts = response.json()["alerts"]
    assert "tickets_sla_overdue" in alerts
    assert isinstance(alerts["tickets_sla_overdue"], int)


# ---- SLA breach sweep -----------------------------------------------------


async def test_mark_sla_breached_stamps_and_appends_note(patient_id):
    """Repo helper stamps the column and writes a structured audit
    note (``[ts] system: SLA breached — sla_minutes=60, age_minutes=…``)
    so ops can scan the timeline to see when the breach was noticed."""
    ticket_id = await _create(patient_id)
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        # Backdate created_at so the helper's age_minutes calculation
        # has something meaningful to record.
        ticket = await ops_tickets_repo.get(db, ticket_id)
        ticket.created_at = datetime.now(timezone.utc) - timedelta(hours=3)
        await db.flush()
        marked = await ops_tickets_repo.mark_sla_breached(db, ticket_id)
        await db.commit()
        assert marked is not None
        assert marked.sla_breached_at is not None
        notes = marked.notes or ""
        assert "system: SLA breached" in notes
        assert "sla_minutes=60" in notes
        # age_minutes is non-zero (we backdated 3h).
        assert "age_minutes=" in notes


async def test_mark_sla_breached_is_idempotent(patient_id):
    """A second mark on an already-breached ticket leaves the
    timestamp untouched and does NOT append a duplicate note."""
    ticket_id = await _create(patient_id)
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        ticket = await ops_tickets_repo.get(db, ticket_id)
        ticket.created_at = datetime.now(timezone.utc) - timedelta(hours=3)
        await db.flush()
        first = await ops_tickets_repo.mark_sla_breached(db, ticket_id)
        await db.commit()
        first_stamp = first.sla_breached_at

        # Second call → no change.
        second = await ops_tickets_repo.mark_sla_breached(db, ticket_id)
        await db.commit()
        assert second.sla_breached_at == first_stamp
        # Only one ``SLA breached`` audit line in the notes.
        assert (second.notes or "").count("SLA breached") == 1


async def test_find_breach_candidates_excludes_snoozed_and_resolved(patient_id):
    """The breach sweep filter must exclude snoozed (clock paused) and
    resolved (clock stopped) tickets even if their raw age would
    otherwise qualify."""
    overdue_id = await _create(patient_id)
    snoozed_id = await _create(patient_id, category="snoozed-cat")
    resolved_id = await _create(patient_id, category="resolved-cat")

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        # Backdate all three so their raw age exceeds the 60-min SLA.
        for tid in (overdue_id, snoozed_id, resolved_id):
            t = await ops_tickets_repo.get(db, tid)
            t.created_at = datetime.now(timezone.utc) - timedelta(hours=3)
        # Snooze one well into the future.
        await ops_tickets_repo.snooze(
            db,
            snoozed_id,
            until=datetime.now(timezone.utc) + timedelta(hours=2),
            actor="ops",
        )
        # Resolve another.
        await ops_tickets_repo.resolve(db, resolved_id, actor="ops")
        await db.commit()

        candidates = await ops_tickets_repo.find_breach_candidates(db)
        ids = {c.id for c in candidates}
        assert overdue_id in ids
        assert snoozed_id not in ids
        assert resolved_id not in ids


async def test_find_breach_candidates_excludes_already_breached(patient_id):
    """Once a ticket has ``sla_breached_at`` set, the sweep must skip
    it on subsequent passes — that's how we ensure each breach is
    audited exactly once."""
    ticket_id = await _create(patient_id)
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        t = await ops_tickets_repo.get(db, ticket_id)
        t.created_at = datetime.now(timezone.utc) - timedelta(hours=3)
        await db.flush()
        await ops_tickets_repo.mark_sla_breached(db, ticket_id)
        await db.commit()

        candidates = await ops_tickets_repo.find_breach_candidates(db)
        assert ticket_id not in {c.id for c in candidates}


async def test_reopen_clears_sla_breached_at(patient_id):
    """Re-opening a previously-breached resolved ticket must clear
    ``sla_breached_at`` so the SLA window restarts. Without this,
    a re-opened ticket would render as breached the moment it
    came back into the queue."""
    ticket_id = await _create(patient_id)
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        t = await ops_tickets_repo.get(db, ticket_id)
        t.created_at = datetime.now(timezone.utc) - timedelta(hours=3)
        await db.flush()
        await ops_tickets_repo.mark_sla_breached(db, ticket_id)
        await ops_tickets_repo.acknowledge(db, ticket_id, actor="ops")
        await ops_tickets_repo.resolve(db, ticket_id, actor="ops")
        await db.commit()

        reopened = await ops_tickets_repo.reopen(
            db, ticket_id, actor="ops", note="needs more attention"
        )
        await db.commit()
        assert reopened.sla_breached_at is None


async def test_sweep_round_trip_marks_overdue_ticket(patient_id):
    """End-to-end: create an overdue ticket, run the sweep, verify
    the column gets stamped and the counter dict reports it."""
    from services.scheduler import sla_breach_sweep

    ticket_id = await _create(patient_id, category="onboarding_stuck")
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        t = await ops_tickets_repo.get(db, ticket_id)
        t.created_at = datetime.now(timezone.utc) - timedelta(hours=3)
        await db.flush()
        await db.commit()

        out = await sla_breach_sweep.sweep_sla_breaches(db)
        await db.commit()

    assert ticket_id in out["breached_ticket_ids"]
    assert out["breached_by_category"].get("onboarding_stuck", 0) >= 1

    async with SessionLocal() as db:
        again = await ops_tickets_repo.get(db, ticket_id)
        assert again.sla_breached_at is not None

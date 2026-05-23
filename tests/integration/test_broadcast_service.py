"""Integration tests for the broadcast campaign flow.

End-to-end against real Postgres because:
    1. Recipient resolution joins multiple cohort + erasure
       columns; correctness is dialect-sensitive.
    2. The materialiser writes to broadcast_sends + scheduled_events
       in the same transaction — partial-failure rollback needs a
       real session.
    3. The /campaigns endpoint round-trip exercises the full stack
       including the eligibility gates + dispatcher event payload.

Skipped when DATABASE_URL is unset.
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import (
    BroadcastSend,
    Patient,
    ScheduledEvent,
)
from app.db.repositories import (
    broadcast_campaigns as broadcast_campaigns_repo,
    patients as patients_repo,
)
from app.db.session import get_sessionmaker
from services.orchestrator import broadcast_service

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping broadcast tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


async def _seed_patient(
    *,
    cohort_diabetes: bool = False,
    cohort_cardiac: bool = False,
    consent_sms: bool = True,
    bot_paused: bool = False,
    erased: bool = False,
    no_phone: bool = False,
) -> tuple[int, str]:
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(
            full_name=f"Broadcast Test {suffix}",
            phone=f"bcast-{suffix}" if not no_phone else "",
            consent_sms=consent_sms,
            cohort_diabetes=cohort_diabetes,
            cohort_cardiac=cohort_cardiac,
        )
        db.add(p)
        await db.flush()

        if bot_paused:
            await patients_repo.pause_bot(
                db, p.id, actor="ops_test", reason="test"
            )
        if erased:
            from services.orchestrator import patient_erasure

            await patient_erasure.erase_patient_data(
                db,
                patient_id=p.id,
                actor="ops_test",
                reason="test erasure",
            )
        await db.commit()
        return p.id, p.phone


async def _create_campaign(
    *,
    cohort: str,
    template_name: str = "test_template_v1",
) -> int:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        c = await broadcast_campaigns_repo.create(
            db,
            name=f"test-campaign-{uuid.uuid4().hex[:6]}",
            template_name=template_name,
            template_params={"1_name": "{{patient.first_name}}"},
            cohort_filter={"cohort": cohort},
            created_by="ops_test",
        )
        await db.commit()
        return c.id


# ---- Recipient resolution -----------------------------------------------


async def test_resolve_recipients_legacy_cohort_filter():
    """Diabetes filter returns only diabetes-cohort patients."""
    pid_dia, _ = await _seed_patient(cohort_diabetes=True)
    pid_card, _ = await _seed_patient(cohort_cardiac=True)
    pid_none, _ = await _seed_patient()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows = await broadcast_service.resolve_recipients(
            db, cohort_filter={"cohort": "diabetes"}
        )

    ids = {p.id for p in rows}
    assert pid_dia in ids
    assert pid_card not in ids
    assert pid_none not in ids


async def test_resolve_recipients_excludes_erased_patients():
    """Erased patients were already wiped of PII — they must NOT
    receive broadcasts even if their cohort flags survived."""
    pid, _ = await _seed_patient(cohort_diabetes=True, erased=True)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows = await broadcast_service.resolve_recipients(
            db, cohort_filter={"cohort": "diabetes"}
        )

    assert pid not in {p.id for p in rows}


async def test_resolve_recipients_unknown_cohort_raises():
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        with pytest.raises(ValueError, match="unsupported cohort"):
            await broadcast_service.resolve_recipients(
                db, cohort_filter={"cohort": "made_up_cohort"}
            )


# ---- Eligibility gates ---------------------------------------------------


async def test_materialise_skips_opted_out_patients():
    """Opted-out patients (consent_sms=False) land in
    broadcast_sends with status=skipped + reason=opted_out."""
    pid_eligible, phone_eligible = await _seed_patient(
        cohort_diabetes=True, consent_sms=True
    )
    pid_optout, _ = await _seed_patient(
        cohort_diabetes=True, consent_sms=False
    )

    campaign_id = await _create_campaign(cohort="diabetes")
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await broadcast_service.materialise_campaign(
            db, campaign_id=campaign_id
        )
        await db.commit()

    # Confirm the breakdown captures the opt-out.
    assert "opted_out" in result["skipped_breakdown"]
    assert result["skipped_breakdown"]["opted_out"] >= 1

    # Confirm the eligible patient got an enqueued event.
    async with SessionLocal() as db:
        sends = await broadcast_campaigns_repo.list_sends(
            db, campaign_id, limit=200
        )
    by_pid = {s.patient_db_id: s for s in sends}
    assert by_pid[pid_eligible].status == "pending"
    assert by_pid[pid_eligible].scheduled_event_id is not None
    assert by_pid[pid_optout].status == "skipped"
    assert by_pid[pid_optout].skip_reason == "opted_out"
    assert by_pid[pid_optout].scheduled_event_id is None


async def test_materialise_skips_paused_patients():
    pid_paused, _ = await _seed_patient(
        cohort_diabetes=True, bot_paused=True
    )
    campaign_id = await _create_campaign(cohort="diabetes")

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await broadcast_service.materialise_campaign(
            db, campaign_id=campaign_id
        )
        await db.commit()
        sends = await broadcast_campaigns_repo.list_sends(
            db, campaign_id, limit=500
        )

    paused = next(s for s in sends if s.patient_db_id == pid_paused)
    assert paused.status == "skipped"
    assert paused.skip_reason == "paused"


async def test_materialise_enqueues_scheduled_event_with_template():
    """Eligible recipients get a scheduled_event with
    event_type=broadcast_send + the campaign's template +
    params in the payload. Confirms the dispatcher's broadcast
    branch will have what it needs to render."""
    pid, phone = await _seed_patient(cohort_diabetes=True)
    campaign_id = await _create_campaign(
        cohort="diabetes", template_name="custom_v1"
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await broadcast_service.materialise_campaign(
            db, campaign_id=campaign_id
        )
        await db.commit()

    async with SessionLocal() as db:
        # Find OUR recipient row.
        send_stmt = (
            select(BroadcastSend)
            .where(BroadcastSend.campaign_id == campaign_id)
            .where(BroadcastSend.patient_db_id == pid)
        )
        send = (await db.execute(send_stmt)).scalar_one()
        assert send.scheduled_event_id is not None

        event = await db.get(ScheduledEvent, send.scheduled_event_id)
        assert event is not None
        assert event.event_type == "broadcast_send"
        assert event.patient_id == phone
        assert event.payload["template_name"] == "custom_v1"
        assert event.payload["campaign_id"] == campaign_id


async def test_materialise_twice_raises():
    """Re-materialising a campaign that's already passed through
    the draft → materialised transition must raise. A second run
    would create duplicate sends, double-enqueue events, and
    confuse ops + the dispatcher."""
    await _seed_patient(cohort_diabetes=True)
    campaign_id = await _create_campaign(cohort="diabetes")

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await broadcast_service.materialise_campaign(
            db, campaign_id=campaign_id
        )
        await db.commit()

    async with SessionLocal() as db:
        with pytest.raises(ValueError, match="not in draft"):
            await broadcast_service.materialise_campaign(
                db, campaign_id=campaign_id
            )


# ---- HTTP endpoint round-trip --------------------------------------------


def test_create_endpoint_round_trip(orchestrator_client):
    """End-to-end: POST /campaigns → recipient list resolved +
    sends materialised + counts populated."""
    import asyncio

    loop = asyncio.get_event_loop()
    suffix = uuid.uuid4().hex[:8]
    eligible_pid, _ = loop.run_until_complete(
        _seed_patient(cohort_diabetes=True, consent_sms=True)
    )
    optout_pid, _ = loop.run_until_complete(
        _seed_patient(cohort_diabetes=True, consent_sms=False)
    )

    r = orchestrator_client.post(
        "/campaigns",
        json={
            "name": f"endpoint-test-{suffix}",
            "template_name": "test_v1",
            "template_params": {"1_name": "patient"},
            "cohort_filter": {"cohort": "diabetes"},
            "created_by": "ops_test",
            "materialise_immediately": True,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["campaign"]["status"] == "materialised"
    assert body["campaign"]["sent_count"] >= 1
    assert body["campaign"]["skipped_count"] >= 1
    assert (
        body["counts_by_skip_reason"].get("opted_out", 0) >= 1
    )


def test_create_endpoint_validates_cohort(orchestrator_client):
    """Bad cohort filter must 400, not 500. We catch the
    ValueError from the service + map cleanly."""
    r = orchestrator_client.post(
        "/campaigns",
        json={
            "name": "bad-cohort",
            "template_name": "x",
            "cohort_filter": {"cohort": "made_up"},
            "created_by": "ops_test",
        },
    )
    assert r.status_code == 400


def test_create_endpoint_validates_required_fields(orchestrator_client):
    """Empty name must 422 — the audit trail (name + created_by
    + reason) is the regulator-visible record."""
    r = orchestrator_client.post(
        "/campaigns",
        json={
            "name": "",
            "template_name": "x",
            "cohort_filter": {"cohort": "diabetes"},
            "created_by": "ops",
        },
    )
    assert r.status_code == 422


def test_get_endpoint_returns_progress_breakdown(orchestrator_client):
    import asyncio

    loop = asyncio.get_event_loop()
    loop.run_until_complete(
        _seed_patient(cohort_cardiac=True, consent_sms=True)
    )

    create_r = orchestrator_client.post(
        "/campaigns",
        json={
            "name": f"progress-test-{uuid.uuid4().hex[:6]}",
            "template_name": "test_v1",
            "cohort_filter": {"cohort": "cardiac"},
            "created_by": "ops",
            "materialise_immediately": True,
        },
    )
    cid = create_r.json()["campaign"]["id"]

    r = orchestrator_client.get(f"/campaigns/{cid}")
    assert r.status_code == 200
    body = r.json()
    assert body["campaign"]["id"] == cid
    assert "counts_by_status" in body
    assert "counts_by_skip_reason" in body


def test_get_recipients_endpoint(orchestrator_client):
    import asyncio

    loop = asyncio.get_event_loop()
    loop.run_until_complete(
        _seed_patient(cohort_diabetes=True, consent_sms=True)
    )

    create_r = orchestrator_client.post(
        "/campaigns",
        json={
            "name": f"recipients-test-{uuid.uuid4().hex[:6]}",
            "template_name": "test_v1",
            "cohort_filter": {"cohort": "diabetes"},
            "created_by": "ops",
            "materialise_immediately": True,
        },
    )
    cid = create_r.json()["campaign"]["id"]

    r = orchestrator_client.get(f"/campaigns/{cid}/recipients")
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list)
    if rows:
        assert "status" in rows[0]
        assert "patient_id" in rows[0]


def test_dispatcher_branch_handles_broadcast_send_payload():
    """The dispatcher's _build_message_out must read template
    name + params from a broadcast_send payload. This is the
    contract between the materialiser and the dispatcher."""
    from services.scheduler.dispatcher import _build_message_out

    fake_event = type(
        "E",
        (),
        {
            "id": 999,
            "event_type": "broadcast_send",
            "patient_id": "+91-test",
            "payload": {
                "campaign_id": 1,
                "template_name": "seasonal_flu_v1",
                "template_params": {"1_name": "Asha"},
            },
        },
    )()
    out = _build_message_out(fake_event)
    assert out["use_template"] is True
    assert out["template_name"] == "seasonal_flu_v1"
    assert out["template_params"]["1_name"] == "Asha"
    assert out["patient_id"] == "+91-test"


def test_dispatcher_broadcast_payload_missing_template_raises():
    """A malformed broadcast_send (missing template_name in
    payload) must raise — the dispatcher's caller catches
    ValueError + maps to a skip prefix. Without this guard
    the broadcast would silently fail."""
    from services.scheduler.dispatcher import _build_message_out

    fake_event = type(
        "E",
        (),
        {
            "id": 999,
            "event_type": "broadcast_send",
            "patient_id": "+91-test",
            "payload": {"campaign_id": 1},
        },
    )()
    with pytest.raises(ValueError, match="missing template_name"):
        _build_message_out(fake_event)

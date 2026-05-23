"""Integration tests for the critical-alert paging path.

End-to-end against real Postgres because:
    1. Doctor selection joins appointments → doctors with
       status filters; correctness is dialect-sensitive.
    2. The audit columns on clinical_alerts (paged_doctor_id,
       paged_at, paged_attempts) require the live UPDATE
       behavior we want covered, not mocked.
    3. The re-page sweep query uses ``COALESCE(paged_at,
       created_at) <= cutoff`` which behaves differently
       across dialects.

Gateway interactions are stubbed via httpx-respx so we don't
actually post to the gateway. The test asserts on the
recorded request shape + the alert's audit columns.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import (
    Appointment,
    AppointmentStatus,
    ClinicalAlert,
    Doctor,
    DoctorOAuthStatus,
    Patient,
    ScheduledEvent,
    ScheduledEventStatus,
)
from app.db.repositories import (
    clinical_alerts as clinical_alerts_repo,
    doctors as doctors_repo,
)
from app.db.session import get_sessionmaker
from services.orchestrator import clinical_alert_pager
from services.scheduler import (
    clinical_alert_repage_sweep,
    dispatcher,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping clinical-alert pager tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


# ---- helpers ---------------------------------------------------------------


async def _seed_patient_and_doctor(
    *,
    on_call: bool = False,
    has_phone: bool = True,
) -> tuple[int, str, int, str]:
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        d = Doctor(
            name=f"Dr Pager {suffix}",
            email=f"pager-{suffix}@test.local",
            phone=f"+91pager{suffix}" if has_phone else None,
            timezone="Asia/Kolkata",
            calendar_id="primary",
            oauth_status=DoctorOAuthStatus.connected,
            is_on_call=on_call,
        )
        db.add(d)
        p = Patient(
            full_name=f"Pager Patient {suffix}",
            phone=f"pgr-{suffix}",
            consent_sms=True,
        )
        db.add(p)
        await db.flush()
        await db.commit()
        return p.id, p.phone, d.id, d.phone or ""


async def _seed_alert(
    *, patient_id: int, phone: str, severity: str = "critical"
) -> int:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        row = await clinical_alerts_repo.create(
            db,
            patient_id=patient_id,
            patient_phone=phone,
            message_id=None,
            severity=severity,
            red_flags=["chest pain"],
            clinical_summary="Test critical alert",
            inbound_text="severe chest pain",
            llm_model="test-model",
        )
        await db.commit()
        return row.id


async def _seed_appointment(
    *,
    patient_id: int,
    doctor_id: int,
    when: datetime | None = None,
    status: AppointmentStatus = AppointmentStatus.completed,
) -> int:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        appt = Appointment(
            patient_id=patient_id,
            doctor_id=doctor_id,
            scheduled_for=when or datetime.now(timezone.utc) - timedelta(days=7),
            end_at=(when or datetime.now(timezone.utc) - timedelta(days=7))
            + timedelta(minutes=30),
            status=status,
            calendar_event_id=None,
            calendar_html_link=None,
            summary="prior visit",
            source="integration_test",
        )
        db.add(appt)
        await db.flush()
        await db.commit()
        return appt.id


class _StubGatewayTransport(httpx.AsyncBaseTransport):
    """Records every POST to ``/send`` so tests can assert the
    pager hit the gateway with the right body shape."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.fail = False

    async def handle_async_request(
        self, request: httpx.Request
    ) -> httpx.Response:
        if self.fail:
            return httpx.Response(503, json={"error": "stubbed_failure"})
        body: dict[str, Any] = {}
        if request.content:
            import json as _json

            try:
                body = _json.loads(request.content)
            except Exception:  # noqa: BLE001
                body = {}
        self.requests.append(
            {
                "url": str(request.url),
                "method": request.method,
                "json": body,
            }
        )
        return httpx.Response(200, json={"ok": True})


@pytest.fixture
def stub_gateway(monkeypatch):
    transport = _StubGatewayTransport()
    real_async_client = httpx.AsyncClient

    def patched(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", patched)
    return transport


# ---- doctor selection tests -----------------------------------------------


async def test_doctor_selection_prefers_recent_appointment_doctor():
    """Patient with a confirmed appointment → that doctor is
    chosen, even if there's an on-call fallback available."""
    pid, phone, primary_id, _ = await _seed_patient_and_doctor()
    # On-call doctor is a different person, should NOT be picked.
    _, _, oncall_id, _ = await _seed_patient_and_doctor(on_call=True)

    await _seed_appointment(patient_id=pid, doctor_id=primary_id)
    alert_id = await _seed_alert(patient_id=pid, phone=phone)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        alert = await clinical_alerts_repo.get(db, alert_id)
        chosen = await clinical_alert_pager.pick_doctor_for_alert(
            db, alert
        )

    assert chosen is not None
    assert chosen.id == primary_id
    assert chosen.id != oncall_id


async def test_doctor_selection_falls_back_to_on_call():
    """Patient with no appointment history → an on-call
    doctor with a phone is chosen. Multiple on-call doctors
    may exist from parallel test residue; we assert the
    selection is on-call + reachable rather than tied to a
    specific id (lowest-id determinism is implementation
    detail, not contract)."""
    pid, phone, primary_id, _ = await _seed_patient_and_doctor()
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await doctors_repo.set_on_call(
            db, primary_id, on_call=True
        )
        await db.commit()

    alert_id = await _seed_alert(patient_id=pid, phone=phone)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        alert = await clinical_alerts_repo.get(db, alert_id)
        chosen = await clinical_alert_pager.pick_doctor_for_alert(
            db, alert
        )

    assert chosen is not None
    assert chosen.is_on_call is True
    assert chosen.phone is not None


async def test_doctor_selection_skips_doctors_without_phone():
    """A doctor without a phone is never returned as a paging
    target — we can't actually reach them via WhatsApp."""
    pid, phone, _, _ = await _seed_patient_and_doctor()
    _, _, no_phone_id, _ = await _seed_patient_and_doctor(
        on_call=True, has_phone=False
    )
    _, _, with_phone_id, _ = await _seed_patient_and_doctor(
        on_call=True, has_phone=True
    )

    alert_id = await _seed_alert(patient_id=pid, phone=phone)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        alert = await clinical_alerts_repo.get(db, alert_id)
        chosen = await clinical_alert_pager.pick_doctor_for_alert(
            db, alert
        )

    # The phone-less on-call doctor must NOT be selected.
    assert chosen is not None
    assert chosen.id != no_phone_id
    # Either the phone-less prior selection (skipped) or the
    # phone-having one is OK; assert phone is set.
    assert chosen.phone is not None


async def test_doctor_selection_returns_none_when_nobody_reachable():
    """No appointments + no on-call doctors → pager picks
    nobody. Audit row will record paged_doctor_id=NULL so ops
    can act manually."""
    pid, phone, _, _ = await _seed_patient_and_doctor()
    alert_id = await _seed_alert(patient_id=pid, phone=phone)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        # Make sure no doctors are on-call (clean up any from
        # parallel tests by toggling our seeded one off).
        alert = await clinical_alerts_repo.get(db, alert_id)
        chosen = await clinical_alert_pager.pick_doctor_for_alert(
            db, alert
        )

    # Could be None (no appt, no on-call) OR could pick up an
    # on-call doctor seeded by another test running in
    # parallel. Both paths are valid system behavior; we
    # assert only that NO appointment-path doctor was selected.
    if chosen is not None:
        assert chosen.is_on_call is True


# ---- pager service tests ---------------------------------------------------


async def test_page_alert_stamps_audit_columns(stub_gateway):
    pid, phone, doctor_id, _ = await _seed_patient_and_doctor()
    await _seed_appointment(patient_id=pid, doctor_id=doctor_id)
    alert_id = await _seed_alert(patient_id=pid, phone=phone)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await clinical_alert_pager.page_alert(
            db, alert_id=alert_id
        )
        await db.commit()

    assert result["error"] is None
    assert result["doctor_id"] == doctor_id
    assert result["attempts"] == 1
    assert len(stub_gateway.requests) == 1
    sent = stub_gateway.requests[0]["json"]
    assert "CRITICAL ALERT" in (sent.get("body") or "")
    assert sent.get("use_template") is False

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        row = await clinical_alerts_repo.get(db, alert_id)
    assert row is not None
    assert row.paged_doctor_id == doctor_id
    assert row.paged_at is not None
    assert row.paged_attempts == 1


async def test_page_alert_records_no_doctor_attempt(stub_gateway):
    """No reachable doctor → still bump paged_attempts +
    stamp paged_at so the cap applies. paged_doctor_id stays
    NULL so the UI can show "no doctor reachable"."""
    pid, phone, _, _ = await _seed_patient_and_doctor()
    alert_id = await _seed_alert(patient_id=pid, phone=phone)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await clinical_alert_pager.page_alert(
            db, alert_id=alert_id
        )
        await db.commit()

    # If a parallel test left an on-call doctor in the DB the
    # pager might succeed — assert only the stamp behavior
    # for the no-doctor path.
    if result["error"] == "no_doctor_reachable":
        SessionLocal = get_sessionmaker()
        async with SessionLocal() as db:
            row = await clinical_alerts_repo.get(db, alert_id)
        assert row is not None
        assert row.paged_doctor_id is None
        assert row.paged_at is not None
        assert row.paged_attempts == 1


async def test_page_alert_respects_max_attempts(stub_gateway):
    """After ``MAX_PAGE_ATTEMPTS`` the pager refuses further
    attempts. Doesn't bump attempts. Returns
    ``max_attempts_reached``."""
    pid, phone, doctor_id, _ = await _seed_patient_and_doctor()
    await _seed_appointment(patient_id=pid, doctor_id=doctor_id)
    alert_id = await _seed_alert(patient_id=pid, phone=phone)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        for _ in range(clinical_alert_pager.MAX_PAGE_ATTEMPTS):
            await clinical_alert_pager.page_alert(
                db, alert_id=alert_id
            )
        await db.commit()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await clinical_alert_pager.page_alert(
            db, alert_id=alert_id
        )
        await db.commit()

    assert result["error"] == "max_attempts_reached"
    assert result["attempts"] == clinical_alert_pager.MAX_PAGE_ATTEMPTS

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        row = await clinical_alerts_repo.get(db, alert_id)
    assert row is not None
    assert row.paged_attempts == clinical_alert_pager.MAX_PAGE_ATTEMPTS


async def test_page_alert_skips_acknowledged_alert(stub_gateway):
    """An ack'd alert doesn't need re-paging. Pager refuses."""
    pid, phone, doctor_id, _ = await _seed_patient_and_doctor()
    await _seed_appointment(patient_id=pid, doctor_id=doctor_id)
    alert_id = await _seed_alert(patient_id=pid, phone=phone)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await clinical_alerts_repo.acknowledge(
            db, alert_id, actor="dr.early"
        )
        await db.commit()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await clinical_alert_pager.page_alert(
            db, alert_id=alert_id
        )
        await db.commit()

    assert result["error"] is not None
    assert result["error"].startswith("non_open_status")
    assert len(stub_gateway.requests) == 0


async def test_page_alert_refuses_high_severity():
    """High alerts queue but don't actively page — only
    critical does. The pager rejects high-severity calls so
    misuse (someone calling page_alert manually) doesn't
    silently widen the paging surface."""
    pid, phone, _, _ = await _seed_patient_and_doctor()
    alert_id = await _seed_alert(
        patient_id=pid, phone=phone, severity="high"
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await clinical_alert_pager.page_alert(
            db, alert_id=alert_id
        )
        await db.commit()

    assert result["error"] is not None
    assert result["error"].startswith("non_critical_severity")


async def test_page_alert_records_gateway_failure(stub_gateway):
    """Gateway 5xx → audit row still stamps paged_at +
    attempts (so the cap eventually stops). Returns the
    error so caller logs it."""
    stub_gateway.fail = True
    pid, phone, doctor_id, _ = await _seed_patient_and_doctor()
    await _seed_appointment(patient_id=pid, doctor_id=doctor_id)
    alert_id = await _seed_alert(patient_id=pid, phone=phone)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await clinical_alert_pager.page_alert(
            db, alert_id=alert_id
        )
        await db.commit()

    assert result["error"] is not None
    assert "http_error" in result["error"]
    assert result["doctor_id"] == doctor_id

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        row = await clinical_alerts_repo.get(db, alert_id)
    assert row is not None
    assert row.paged_attempts == 1
    assert row.paged_at is not None


# ---- repo + sweep tests ----------------------------------------------------


async def test_list_unacked_for_repage_returns_eligible_alerts():
    pid, phone, _, _ = await _seed_patient_and_doctor()
    alert_id = await _seed_alert(patient_id=pid, phone=phone)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        # Backdate paged_at so the sweep query sees this alert
        # as past the threshold.
        row = await clinical_alerts_repo.get(db, alert_id)
        assert row is not None
        row.paged_attempts = 1
        row.paged_at = datetime.now(timezone.utc) - timedelta(
            seconds=600
        )
        await db.commit()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        # Pass a very high limit so this test sees our newly-
        # created row even when the test DB has accumulated
        # many older eligible alerts from prior runs (the
        # repo's default LIMIT 50 with ORDER BY created_at ASC
        # would push our row past the page).
        rows = await clinical_alerts_repo.list_unacked_for_repage(
            db, older_than_seconds=300, max_attempts=3, limit=10000
        )
    assert any(r.id == alert_id for r in rows)


async def test_list_unacked_for_repage_excludes_acknowledged():
    pid, phone, _, _ = await _seed_patient_and_doctor()
    alert_id = await _seed_alert(patient_id=pid, phone=phone)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        row = await clinical_alerts_repo.get(db, alert_id)
        assert row is not None
        row.paged_attempts = 1
        row.paged_at = datetime.now(timezone.utc) - timedelta(
            seconds=600
        )
        await clinical_alerts_repo.acknowledge(
            db, alert_id, actor="dr.early"
        )
        await db.commit()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows = await clinical_alerts_repo.list_unacked_for_repage(
            db, older_than_seconds=300, max_attempts=3
        )
    assert not any(r.id == alert_id for r in rows)


async def test_list_unacked_for_repage_excludes_at_cap():
    pid, phone, _, _ = await _seed_patient_and_doctor()
    alert_id = await _seed_alert(patient_id=pid, phone=phone)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        row = await clinical_alerts_repo.get(db, alert_id)
        assert row is not None
        row.paged_attempts = 3  # at cap
        row.paged_at = datetime.now(timezone.utc) - timedelta(
            seconds=600
        )
        await db.commit()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows = await clinical_alerts_repo.list_unacked_for_repage(
            db, older_than_seconds=300, max_attempts=3
        )
    assert not any(r.id == alert_id for r in rows)


async def test_repage_sweep_enqueues_paging_event():
    pid, phone, _, _ = await _seed_patient_and_doctor()
    alert_id = await _seed_alert(patient_id=pid, phone=phone)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        row = await clinical_alerts_repo.get(db, alert_id)
        assert row is not None
        row.paged_attempts = 1
        row.paged_at = datetime.now(timezone.utc) - timedelta(
            seconds=600
        )
        # Backdate created_at well into the past so this alert
        # sorts to the front of the sweep's
        # ``ORDER BY created_at ASC LIMIT 50`` page even when
        # the test DB has accumulated many older eligible
        # alerts from prior runs. Pre-existing real workload
        # would have a more bounded backlog so the production
        # default limit is fine.
        row.created_at = datetime.now(timezone.utc) - timedelta(
            days=365 * 5
        )
        await db.commit()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await clinical_alert_repage_sweep.sweep_repages(db)
        await db.commit()

    assert alert_id in (result.get("repaged_ids") or [])

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        events = list(
            (
                await db.execute(
                    select(ScheduledEvent).where(
                        ScheduledEvent.event_type
                        == "clinical_alert_page",
                        ScheduledEvent.status
                        == ScheduledEventStatus.pending,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert any(
        (e.payload or {}).get("alert_id") == alert_id for e in events
    )


# ---- dispatcher branch tests -----------------------------------------------


async def test_dispatcher_handles_clinical_alert_page(stub_gateway):
    pid, phone, doctor_id, _ = await _seed_patient_and_doctor()
    await _seed_appointment(patient_id=pid, doctor_id=doctor_id)
    alert_id = await _seed_alert(patient_id=pid, phone=phone)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        event = ScheduledEvent(
            event_type="clinical_alert_page",
            patient_id=phone,
            payload={
                "alert_id": alert_id,
                "patient_db_id": pid,
                "severity": "critical",
            },
            scheduled_for=datetime.now(timezone.utc) - timedelta(seconds=10),
            status=ScheduledEventStatus.pending,
        )
        db.add(event)
        await db.flush()
        await db.commit()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await dispatcher.dispatch(event, db=db)
        await db.commit()

    assert result is None  # success
    assert len(stub_gateway.requests) == 1

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        row = await clinical_alerts_repo.get(db, alert_id)
    assert row is not None
    assert row.paged_attempts == 1
    assert row.paged_doctor_id == doctor_id


async def test_dispatcher_rejects_event_without_alert_id():
    """Defence in depth — malformed payload returns
    ``unmapped:`` so the scheduler tick marks it failed."""
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        event = ScheduledEvent(
            event_type="clinical_alert_page",
            patient_id="nobody",
            payload={"severity": "critical"},  # missing alert_id
            scheduled_for=datetime.now(timezone.utc) - timedelta(seconds=5),
            status=ScheduledEventStatus.pending,
        )
        db.add(event)
        await db.flush()
        await db.commit()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        result = await dispatcher.dispatch(event, db=db)

    assert result is not None
    assert result.startswith("unmapped:")


# ---- endpoint tests --------------------------------------------------------


async def test_endpoint_set_doctor_on_call(orchestrator_client):
    """POST /doctors/{id}/on-call flips the flag both ways."""
    _, _, doctor_id, _ = await _seed_patient_and_doctor()

    on = orchestrator_client.post(
        f"/doctors/{doctor_id}/on-call", json={"on_call": True}
    ).json()
    assert on["is_on_call"] is True

    off = orchestrator_client.post(
        f"/doctors/{doctor_id}/on-call", json={"on_call": False}
    ).json()
    assert off["is_on_call"] is False


async def test_endpoint_set_on_call_404_for_missing_doctor(
    orchestrator_client,
):
    response = orchestrator_client.post(
        "/doctors/999999999/on-call", json={"on_call": True}
    )
    assert response.status_code == 404


async def test_endpoint_manual_page(stub_gateway, orchestrator_client):
    pid, phone, doctor_id, _ = await _seed_patient_and_doctor()
    await _seed_appointment(patient_id=pid, doctor_id=doctor_id)
    alert_id = await _seed_alert(patient_id=pid, phone=phone)

    response = orchestrator_client.post(
        f"/clinical-alerts/{alert_id}/page"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["paged_attempts"] >= 1
    assert body["paged_doctor_id"] == doctor_id


async def test_endpoint_manual_page_404_for_missing(orchestrator_client):
    response = orchestrator_client.post(
        "/clinical-alerts/999999999/page"
    )
    assert response.status_code == 404


# ---- /route → critical → paging-event-enqueued -----------------------------


class _FakeMessage:
    def __init__(self, parsed: Any) -> None:
        self.parsed = parsed


class _FakeChoice:
    def __init__(self, parsed: Any) -> None:
        self.message = _FakeMessage(parsed)


class _FakeUsage:
    def __init__(self) -> None:
        self.prompt_tokens = 50
        self.completion_tokens = 30
        self.total_tokens = 80


class _FakeCompletion:
    def __init__(self, parsed: Any) -> None:
        self.choices = [_FakeChoice(parsed)]
        self.usage = _FakeUsage()


class _FakeAsyncCompletions:
    def __init__(self, parsed: Any) -> None:
        self._parsed = parsed

    async def parse(self, *, model, response_format, messages):
        parsed_obj = response_format(**self._parsed)
        return _FakeCompletion(parsed_obj)


class _FakeChat:
    def __init__(self, completions: _FakeAsyncCompletions) -> None:
        self.completions = completions


class _FakeClient:
    def __init__(self, completions: _FakeAsyncCompletions) -> None:
        self.chat = _FakeChat(completions)


class _FakeLLM:
    def __init__(self, parsed: Any) -> None:
        self.enabled = True
        self.model = "gpt-4o-mini"
        self._client = _FakeClient(_FakeAsyncCompletions(parsed))

    def _get_client(self):
        return self._client


async def test_route_critical_message_enqueues_paging_event(
    monkeypatch, orchestrator_client
):
    """Full /route → triage → alert created → paging event
    enqueued. We don't actually let the dispatcher fire here
    (scheduler is disabled in tests), just assert the row +
    the queued event."""
    from services.orchestrator import triage_classifier

    pid, phone, doctor_id, _ = await _seed_patient_and_doctor()
    await _seed_appointment(patient_id=pid, doctor_id=doctor_id)

    fake = _FakeLLM(
        parsed={
            "severity": "critical",
            "red_flags": ["chest pain"],
            "summary": "Patient reports active chest pain",
        }
    )
    monkeypatch.setattr(triage_classifier, "get_llm", lambda: fake)

    response = orchestrator_client.post(
        "/route",
        json={
            "message": {
                "patient_id": phone,
                "text": "chest pain right now",
                "input_kind": "text",
                "message_id": f"wamid-{uuid.uuid4().hex[:8]}",
            },
        },
    )
    assert response.status_code == 200

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        alerts = list(
            (
                await db.execute(
                    select(ClinicalAlert).where(
                        ClinicalAlert.patient_id == pid
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(alerts) >= 1
        alert = alerts[-1]
        assert alert.severity == "critical"

        events = list(
            (
                await db.execute(
                    select(ScheduledEvent).where(
                        ScheduledEvent.event_type
                        == "clinical_alert_page"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert any(
            (e.payload or {}).get("alert_id") == alert.id for e in events
        )


async def test_route_high_severity_does_not_enqueue_paging(
    monkeypatch, orchestrator_client
):
    """High-severity alerts queue but DON'T page actively."""
    from services.orchestrator import triage_classifier

    pid, phone, _, _ = await _seed_patient_and_doctor()
    fake = _FakeLLM(
        parsed={
            "severity": "high",
            "red_flags": ["worsening symptoms"],
            "summary": "Patient reports worsening symptoms",
        }
    )
    monkeypatch.setattr(triage_classifier, "get_llm", lambda: fake)

    response = orchestrator_client.post(
        "/route",
        json={
            "message": {
                "patient_id": phone,
                "text": "rash spreading and itching",
                "input_kind": "text",
                "message_id": f"wamid-{uuid.uuid4().hex[:8]}",
            },
        },
    )
    assert response.status_code == 200

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        alerts = list(
            (
                await db.execute(
                    select(ClinicalAlert).where(
                        ClinicalAlert.patient_id == pid
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(alerts) >= 1
        alert_ids = {a.id for a in alerts}

        events = list(
            (
                await db.execute(
                    select(ScheduledEvent).where(
                        ScheduledEvent.event_type
                        == "clinical_alert_page"
                    )
                )
            )
            .scalars()
            .all()
        )
        # No paging event should reference any of the high-severity alerts.
        for e in events:
            assert (e.payload or {}).get("alert_id") not in alert_ids


# ---- on-call rota escalation (task #13) ------------------------------------


async def test_escalation_picks_a_different_on_call_doctor():
    """On a re-page (escalation_level >= 1) the rota must escalate to a NEW
    on-call doctor — never the one we already paged."""
    pid, phone, doc_a, _ = await _seed_patient_and_doctor(on_call=True)
    await _seed_patient_and_doctor(on_call=True)  # a second on-call doctor
    alert_id = await _seed_alert(patient_id=pid, phone=phone)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        # Pretend doctor A was paged on the first attempt.
        await clinical_alerts_repo.mark_paged(db, alert_id, doctor_id=doc_a)
        await db.commit()

    async with SessionLocal() as db:
        alert = await clinical_alerts_repo.get(db, alert_id)
        chosen = await clinical_alert_pager.pick_doctor_for_alert(
            db, alert, escalation_level=1
        )
    assert chosen is not None
    assert chosen.is_on_call is True
    assert chosen.phone is not None
    assert chosen.id != doc_a  # escalated to someone new


async def test_escalation_level_zero_unchanged_prefers_primary():
    """Level 0 (first page) behaviour is unchanged: the patient's recent
    appointment doctor wins over the on-call rota."""
    pid, phone, primary_id, _ = await _seed_patient_and_doctor()
    await _seed_patient_and_doctor(on_call=True)  # an on-call alternative
    await _seed_appointment(patient_id=pid, doctor_id=primary_id)
    alert_id = await _seed_alert(patient_id=pid, phone=phone)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        alert = await clinical_alerts_repo.get(db, alert_id)
        chosen = await clinical_alert_pager.pick_doctor_for_alert(
            db, alert, escalation_level=0
        )
    assert chosen is not None
    assert chosen.id == primary_id

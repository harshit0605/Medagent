"""Integration tests for slice-12 follow-ups: clinical-alert
integration into the timeline aggregator + inbox-row severity
denormalisation.

Covered:
    1. Timeline aggregator emits ``clinical_alert_high`` and
       ``clinical_alert_critical`` events for alerts in window.
    2. Defensive: alerts with unexpected severity (legacy /
       backfill) are silently skipped — never miscoded as high
       or critical.
    3. The /route hook stamps ``clinical_severity`` on the
       inbound_classifications row when the triage classifier
       produced an alert. Visible via the /ops/inbox endpoint.
    4. Routine messages (no alert raised) leave
       ``clinical_severity`` NULL — inbox view degrades to "no
       badge" rather than showing a stale value.

LLM is stubbed via the same _FakeLLM pattern from prior
slices; DATABASE_URL is required (skipped otherwise).
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.models import (
    ClinicalAlert,
    InboundClassification,
    Patient,
)
from app.db.repositories import (
    clinical_alerts as clinical_alerts_repo,
)
from app.db.session import get_sessionmaker
from services.orchestrator import patient_timeline, triage_classifier

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping followup tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


# ---- LLM stub --------------------------------------------------------------


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


# ---- helpers ---------------------------------------------------------------


async def _seed_patient() -> tuple[int, str]:
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(
            full_name=f"Followup Test {suffix}",
            phone=f"flw-{suffix}",
            consent_sms=True,
        )
        db.add(p)
        await db.flush()
        await db.commit()
        return p.id, p.phone


async def _seed_alert(
    *,
    patient_id: int,
    phone: str,
    severity: str = "critical",
    summary: str = "Test alert",
    inbound: str = "test message",
    red_flags: list[str] | None = None,
) -> int:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        row = await clinical_alerts_repo.create(
            db,
            patient_id=patient_id,
            patient_phone=phone,
            message_id=None,
            severity=severity,
            red_flags=red_flags or ["test_flag"],
            clinical_summary=summary,
            inbound_text=inbound,
            llm_model="test-model",
        )
        await db.commit()
        return row.id


# ---- timeline aggregator tests ---------------------------------------------


async def test_timeline_includes_critical_alert_event():
    """A critical alert in the window surfaces as a
    ``clinical_alert_critical`` timeline event with summary
    + red_flags + status carried in detail."""
    pid, phone = await _seed_patient()
    alert_id = await _seed_alert(
        patient_id=pid,
        phone=phone,
        severity="critical",
        summary="Patient reports active chest pain at rest",
        red_flags=["chest pain", "shortness of breath"],
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        events = await patient_timeline.build_timeline(
            db,
            patient_db_id=pid,
            since=datetime.now(timezone.utc) - timedelta(days=1),
            until=datetime.now(timezone.utc) + timedelta(seconds=60),
            kinds=("clinical_alert_critical",),
        )

    assert len(events) == 1
    e = events[0]
    assert e.kind == "clinical_alert_critical"
    assert e.title.startswith("🚨")
    assert "chest pain" in (e.body or "")
    assert e.detail["alert_id"] == alert_id
    assert "chest pain" in e.detail["red_flags"]
    assert e.detail["status"] == "open"


async def test_timeline_includes_high_alert_event():
    pid, phone = await _seed_patient()
    await _seed_alert(
        patient_id=pid,
        phone=phone,
        severity="high",
        summary="Worsening symptom report",
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        events = await patient_timeline.build_timeline(
            db,
            patient_db_id=pid,
            since=datetime.now(timezone.utc) - timedelta(days=1),
            until=datetime.now(timezone.utc) + timedelta(seconds=60),
            kinds=("clinical_alert_high",),
        )

    assert len(events) == 1
    assert events[0].kind == "clinical_alert_high"
    assert events[0].title.startswith("⚠️")


async def test_timeline_filters_alerts_outside_window():
    pid, phone = await _seed_patient()
    alert_id = await _seed_alert(patient_id=pid, phone=phone)

    # Bump created_at way back so the window excludes it.
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        row = await clinical_alerts_repo.get(db, alert_id)
        assert row is not None
        row.created_at = datetime.now(timezone.utc) - timedelta(days=60)
        await db.commit()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        events = await patient_timeline.build_timeline(
            db,
            patient_db_id=pid,
            since=datetime.now(timezone.utc) - timedelta(days=30),
            until=datetime.now(timezone.utc) + timedelta(seconds=60),
            kinds=(
                "clinical_alert_high",
                "clinical_alert_critical",
            ),
        )

    assert events == []


async def test_timeline_skips_unknown_severity_alerts():
    """Defensive: an alert row with a severity string we don't
    recognise (legacy data, future enum value) is silently
    skipped from the timeline rather than being miscoded as
    high or critical."""
    pid, phone = await _seed_patient()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        row = ClinicalAlert(
            patient_id=pid,
            patient_phone=phone,
            severity="urgent",  # not in our taxonomy
            red_flags=[],
            clinical_summary="legacy",
            inbound_text="legacy",
            status="open",
        )
        db.add(row)
        await db.commit()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        events = await patient_timeline.build_timeline(
            db,
            patient_db_id=pid,
            since=datetime.now(timezone.utc) - timedelta(days=1),
            until=datetime.now(timezone.utc) + timedelta(seconds=60),
            kinds=(
                "clinical_alert_high",
                "clinical_alert_critical",
            ),
        )

    # Neither high nor critical kinds should pick up the
    # legacy row.
    assert events == []


async def test_timeline_kind_taxonomy_includes_alert_kinds():
    """Both alert kinds are in ALL_KINDS so the endpoint's
    kind-validation accepts them."""
    assert "clinical_alert_high" in patient_timeline.ALL_KINDS
    assert (
        "clinical_alert_critical" in patient_timeline.ALL_KINDS
    )


# ---- inbox enrichment tests ------------------------------------------------


async def test_inbox_row_gets_clinical_severity_when_alert_fires(
    monkeypatch, orchestrator_client
):
    """Hit /route with a critical-severity stub. The inbound
    classification row that lands should have
    ``clinical_severity='critical'`` so the inbox UI badge
    renders."""
    pid, phone = await _seed_patient()

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
        rows = list(
            (
                await db.execute(
                    select(InboundClassification).where(
                        InboundClassification.patient_phone == phone
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) >= 1
    latest = rows[-1]
    assert latest.clinical_severity == "critical"


async def test_inbox_row_clinical_severity_null_for_routine(
    monkeypatch, orchestrator_client
):
    """Routine message — triage classifier returns ``low`` —
    the inbox row's ``clinical_severity`` stays NULL."""
    pid, phone = await _seed_patient()

    fake = _FakeLLM(
        parsed={
            "severity": "low",
            "red_flags": [],
            "summary": "(routine)",
        }
    )
    monkeypatch.setattr(triage_classifier, "get_llm", lambda: fake)

    response = orchestrator_client.post(
        "/route",
        json={
            "message": {
                "patient_id": phone,
                "text": "thanks doc, see you next week",
                "input_kind": "text",
                "message_id": f"wamid-{uuid.uuid4().hex[:8]}",
            },
        },
    )
    assert response.status_code == 200

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        rows = list(
            (
                await db.execute(
                    select(InboundClassification).where(
                        InboundClassification.patient_phone == phone
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) >= 1
    latest = rows[-1]
    assert latest.clinical_severity is None


async def test_inbox_endpoint_surfaces_clinical_severity(
    monkeypatch, orchestrator_client
):
    """/ops/inbox DTO carries clinical_severity through so the
    UI can render the badge without a second fetch."""
    pid, phone = await _seed_patient()

    fake = _FakeLLM(
        parsed={
            "severity": "high",
            "red_flags": ["worsening symptoms"],
            "summary": "Patient reports worsening rash",
        }
    )
    monkeypatch.setattr(triage_classifier, "get_llm", lambda: fake)

    orchestrator_client.post(
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

    response = orchestrator_client.get(
        f"/ops/inbox?patient_phone={phone}"
    )
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) >= 1
    severities = [r.get("clinical_severity") for r in rows]
    assert "high" in severities


# ---- patient detail endpoint test ------------------------------------------


async def test_patient_alerts_endpoint_returns_in_newest_first(
    orchestrator_client,
):
    pid, phone = await _seed_patient()
    older = await _seed_alert(patient_id=pid, phone=phone)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        row = await clinical_alerts_repo.get(db, older)
        assert row is not None
        row.created_at = datetime.now(timezone.utc) - timedelta(hours=2)
        await db.commit()

    newer = await _seed_alert(
        patient_id=pid, phone=phone, severity="high"
    )

    response = orchestrator_client.get(
        f"/patients/{pid}/clinical-alerts"
    )
    assert response.status_code == 200
    rows = response.json()
    ids = [r["id"] for r in rows]
    assert ids[0] == newer  # newest first
    assert older in ids

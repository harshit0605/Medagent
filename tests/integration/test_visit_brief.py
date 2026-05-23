"""Integration tests for the visit brief generator + endpoints.

End-to-end against real Postgres because:
    1. The generator pulls timeline data from 5 sources +
       persists to a 6th — a unit test would have to stub all
       of them and we'd lose the integration value.
    2. The ``supersede_for_appointment`` helper depends on
       Postgres' UPDATE row-count semantics; SQLite drift is
       known.
    3. The endpoint round-trip (status flip on first GET) is
       exactly the kind of stateful behaviour we want covered
       by an actual session, not a mock.

LLM behaviour: tests with the default ``LLM_ENABLED=0`` env
exercise the disabled-LLM placeholder path. A separate test
stubs ``get_llm`` to verify the LLM path produces structured
output and that an LLM failure records a failed-brief row.

Skipped when DATABASE_URL is unset.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.db.models import (
    AdherenceEvent,
    AdherenceStatus,
    Patient,
)
from app.db.repositories import (
    message_log as message_log_repo,
    ops_tickets as ops_tickets_repo,
    regimens as regimens_repo,
    visit_briefs as visit_briefs_repo,
)
from app.db.session import get_sessionmaker
from services.orchestrator import visit_brief_generator

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping visit-brief tests",
)


@pytest.fixture(scope="module")
def orchestrator_client():
    from services.orchestrator.main import app

    with TestClient(app) as client:
        yield client


# ---- helpers ---------------------------------------------------------------


async def _seed_patient() -> tuple[int, str]:
    suffix = uuid.uuid4().hex[:8]
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        p = Patient(
            full_name=f"Brief Test {suffix}",
            phone=f"vb-{suffix}",
            consent_sms=True,
        )
        db.add(p)
        await db.flush()
        await db.commit()
        return p.id, p.phone


async def _seed_regimen(patient_id: int) -> int:
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        r = await regimens_repo.create(
            db,
            patient_id=patient_id,
            medication_name="Metformin",
            dose="500mg",
            schedule={
                "type": "times_of_day",
                "times": ["08:00"],
                "timezone": "Asia/Kolkata",
                "frequency": "daily",
            },
            strict_timing=False,
        )
        await db.commit()
        return r.id


async def _seed_dose(
    *,
    patient_id: int,
    regimen_id: int,
    when: datetime,
    status: AdherenceStatus,
):
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        row = AdherenceEvent(
            patient_id=patient_id,
            regimen_id=regimen_id,
            scheduled_at=when,
            status=status,
            confirmed_at=when if status != AdherenceStatus.missed else None,
        )
        db.add(row)
        await db.commit()


async def _seed_inbound(*, phone: str, body: str, when: datetime):
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await message_log_repo.append_inbound(
            db,
            patient_id=phone,
            payload={"type": "text", "text": {"body": body}},
            occurred_at=when,
        )
        await db.commit()


async def _seed_side_effect(*, phone: str, said: str):
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await ops_tickets_repo.create(
            db,
            patient_id=phone,
            category="side_effect_report",
            priority="high",
            sla_minutes=60,
            notes=f"patient said: {said}",
        )
        await db.commit()


# ---- generator tests (disabled-LLM path) -----------------------------------


async def test_generate_brief_disabled_llm_produces_placeholder():
    """With LLM disabled (the default test env), the generator
    still persists a row with computed metrics and a
    placeholder summary so the doctor sees *something* even in
    LLM-down conditions."""
    pid, phone = await _seed_patient()
    regimen_id = await _seed_regimen(pid)
    now = datetime.now(timezone.utc)

    await _seed_dose(
        patient_id=pid,
        regimen_id=regimen_id,
        when=now - timedelta(hours=2),
        status=AdherenceStatus.taken,
    )
    await _seed_dose(
        patient_id=pid,
        regimen_id=regimen_id,
        when=now - timedelta(hours=26),
        status=AdherenceStatus.missed,
    )
    await _seed_inbound(
        phone=phone, body="hi", when=now - timedelta(hours=1)
    )
    await _seed_side_effect(phone=phone, said="bad headache")

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        brief = await visit_brief_generator.generate_brief(
            db, patient_id=pid, generated_by="ops_test"
        )
        await db.commit()

    assert brief.id is not None
    assert "LLM disabled" in brief.summary
    assert brief.key_metrics["doses_taken"] == 1
    assert brief.key_metrics["doses_missed"] == 1
    assert brief.key_metrics["doses_total"] == 2
    assert brief.key_metrics["adherence_rate"] == 0.5
    assert brief.key_metrics["side_effect_count"] == 1
    assert brief.key_metrics["messages_inbound"] == 1
    assert brief.status == "draft"
    assert brief.error is None
    assert brief.generated_by == "ops_test"


async def test_generate_brief_invalid_window_days_raises():
    pid, _ = await _seed_patient()
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        with pytest.raises(ValueError, match="window_days"):
            await visit_brief_generator.generate_brief(
                db, patient_id=pid, window_days=0
            )
        with pytest.raises(ValueError, match="window_days"):
            await visit_brief_generator.generate_brief(
                db, patient_id=pid, window_days=500
            )


async def test_generate_brief_unknown_patient_raises():
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        with pytest.raises(ValueError, match="not found"):
            await visit_brief_generator.generate_brief(
                db, patient_id=999_999_999
            )


async def test_generate_brief_no_doses_yields_null_adherence_rate():
    """Patient with zero dose events shouldn't divide-by-zero;
    adherence_rate is None and the UI knows to render '—'."""
    pid, _ = await _seed_patient()
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        brief = await visit_brief_generator.generate_brief(
            db, patient_id=pid
        )
        await db.commit()
    assert brief.key_metrics["adherence_rate"] is None
    assert brief.key_metrics["doses_total"] == 0


# ---- generator tests (stubbed LLM path) ------------------------------------


class _FakeMessage:
    def __init__(self, parsed: Any) -> None:
        self.parsed = parsed


class _FakeChoice:
    def __init__(self, parsed: Any) -> None:
        self.message = _FakeMessage(parsed)


class _FakeUsage:
    def __init__(self, prompt: int, completion: int) -> None:
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = prompt + completion


class _FakeCompletion:
    def __init__(self, parsed: Any) -> None:
        self.choices = [_FakeChoice(parsed)]
        self.usage = _FakeUsage(120, 80)


class _FakeAsyncCompletions:
    def __init__(self, parsed: Any, *, raise_exc: Exception | None = None) -> None:
        self._parsed = parsed
        self._raise = raise_exc

    async def parse(self, *, model, response_format, messages):
        if self._raise is not None:
            raise self._raise
        # Coerce dict to the response_format pydantic model so
        # downstream code that calls ``.summary`` etc. works.
        parsed_obj = response_format(**self._parsed)
        return _FakeCompletion(parsed_obj)


class _FakeChat:
    def __init__(self, completions: _FakeAsyncCompletions) -> None:
        self.completions = completions


class _FakeClient:
    def __init__(self, completions: _FakeAsyncCompletions) -> None:
        self.chat = _FakeChat(completions)


class _FakeLLM:
    def __init__(
        self,
        parsed: Any,
        *,
        raise_exc: Exception | None = None,
    ) -> None:
        self.enabled = True
        self.model = "gpt-4o-mini"
        self._client = _FakeClient(
            _FakeAsyncCompletions(parsed, raise_exc=raise_exc)
        )

    def _get_client(self):
        return self._client


async def test_generate_brief_with_llm_persists_structured_output(
    monkeypatch,
):
    """Stub the LLM to return a known structured payload — the
    generator must persist summary/talking_points/red_flags
    verbatim plus the locally-computed metrics."""
    pid, _ = await _seed_patient()
    regimen_id = await _seed_regimen(pid)
    now = datetime.now(timezone.utc)
    await _seed_dose(
        patient_id=pid,
        regimen_id=regimen_id,
        when=now - timedelta(hours=2),
        status=AdherenceStatus.taken,
    )

    fake = _FakeLLM(
        parsed={
            "summary": "Patient took their morning dose. Engagement steady.",
            "talking_points": [
                "Confirm evening dose timing",
                "Ask about sleep quality",
            ],
            "red_flags": [],
        }
    )
    monkeypatch.setattr(
        visit_brief_generator, "get_llm", lambda: fake
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        brief = await visit_brief_generator.generate_brief(
            db, patient_id=pid, generated_by="ops_llm"
        )
        await db.commit()

    assert "morning dose" in brief.summary
    assert len(brief.talking_points) == 2
    assert "Confirm evening dose timing" in brief.talking_points
    assert brief.red_flags == []
    assert brief.prompt_tokens == 120
    assert brief.completion_tokens == 80
    assert brief.error is None


async def test_generate_brief_llm_failure_records_failed_row(
    monkeypatch,
):
    """When the LLM raises, the generator persists a failed
    brief row + re-raises RuntimeError so the caller can map
    to a 502."""
    pid, _ = await _seed_patient()

    fake = _FakeLLM(
        parsed={"summary": "", "talking_points": [], "red_flags": []},
        raise_exc=ConnectionError("simulated network down"),
    )
    monkeypatch.setattr(
        visit_brief_generator, "get_llm", lambda: fake
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        with pytest.raises(RuntimeError, match="visit brief"):
            await visit_brief_generator.generate_brief(
                db, patient_id=pid, generated_by="ops_test"
            )
        await db.commit()

    # Failed row must be persisted even though the call raised.
    SessionLocal2 = get_sessionmaker()
    async with SessionLocal2() as db:
        rows = await visit_briefs_repo.list_for_patient(
            db, pid, include_failed=True
        )
    assert len(rows) >= 1
    failed = [r for r in rows if r.error is not None]
    assert len(failed) == 1
    assert "simulated network down" in failed[0].error


async def test_generate_brief_truncates_oversized_lists(monkeypatch):
    """The LLM might return more talking_points / red_flags
    than the cap allows — the generator must clamp to the
    documented limits before persisting."""
    pid, _ = await _seed_patient()

    fake = _FakeLLM(
        parsed={
            "summary": "Test summary.",
            "talking_points": [f"point #{i}" for i in range(20)],
            "red_flags": [f"flag #{i}" for i in range(10)],
        }
    )
    monkeypatch.setattr(
        visit_brief_generator, "get_llm", lambda: fake
    )

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        brief = await visit_brief_generator.generate_brief(
            db, patient_id=pid
        )
        await db.commit()

    assert len(brief.talking_points) == visit_brief_generator.MAX_TALKING_POINTS
    assert len(brief.red_flags) == visit_brief_generator.MAX_RED_FLAGS


# ---- repo / supersede tests ------------------------------------------------


async def test_supersede_marks_earlier_briefs_for_appointment():
    """When two briefs land for the same appointment_id, the
    older ones flip to ``superseded`` so per-appointment
    queries get the latest unambiguously."""
    from app.db.models import Appointment, AppointmentStatus, Doctor, DoctorOAuthStatus

    pid, _ = await _seed_patient()

    # Need an appointment to anchor briefs to.
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        d = Doctor(
            name=f"Dr Brief {uuid.uuid4().hex[:6]}",
            email=f"brief-{uuid.uuid4().hex[:6]}@x.test",
            timezone="UTC",
            calendar_id="primary",
            oauth_status=DoctorOAuthStatus.disconnected,
        )
        db.add(d)
        await db.flush()
        appt = Appointment(
            patient_id=pid,
            doctor_id=d.id,
            scheduled_for=datetime.now(timezone.utc) + timedelta(hours=2),
            end_at=datetime.now(timezone.utc) + timedelta(hours=2, minutes=30),
            status=AppointmentStatus.confirmed,
        )
        db.add(appt)
        await db.flush()
        appt_id = appt.id
        await db.commit()

    # First brief
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        brief_a = await visit_brief_generator.generate_brief(
            db, patient_id=pid, appointment_id=appt_id
        )
        await db.commit()

    # Second brief for the same appointment supersedes the first
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        brief_b = await visit_brief_generator.generate_brief(
            db, patient_id=pid, appointment_id=appt_id
        )
        await db.commit()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        old = await visit_briefs_repo.get(db, brief_a.id)
        new = await visit_briefs_repo.get(db, brief_b.id)
    assert old is not None
    assert new is not None
    assert old.status == "superseded"
    assert new.status == "draft"


# ---- endpoint tests --------------------------------------------------------


async def test_endpoint_generate_round_trip(orchestrator_client):
    pid, _ = await _seed_patient()
    response = orchestrator_client.post(
        f"/patients/{pid}/visit-briefs/generate",
        json={"window_days": 30, "generated_by": "ops_endpoint"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["patient_id"] == pid
    assert data["status"] == "draft"
    assert "summary" in data
    assert "key_metrics" in data
    assert data["generated_by"] == "ops_endpoint"


async def test_endpoint_generate_validates_window_days(orchestrator_client):
    pid, _ = await _seed_patient()
    response = orchestrator_client.post(
        f"/patients/{pid}/visit-briefs/generate",
        json={"window_days": 999},
    )
    # Pydantic Field constraint catches this as 422 before we
    # hit the application-level validation. Either is fine —
    # the contract is "rejected".
    assert response.status_code in (400, 422)


async def test_endpoint_generate_404_for_missing_patient(
    orchestrator_client,
):
    response = orchestrator_client.post(
        "/patients/999999999/visit-briefs/generate", json={}
    )
    assert response.status_code == 404


async def test_endpoint_list_returns_newest_first(orchestrator_client):
    pid, _ = await _seed_patient()
    # Generate two briefs
    orchestrator_client.post(
        f"/patients/{pid}/visit-briefs/generate", json={}
    )
    orchestrator_client.post(
        f"/patients/{pid}/visit-briefs/generate", json={}
    )

    response = orchestrator_client.get(
        f"/patients/{pid}/visit-briefs"
    )
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) >= 2
    # Newest-first: first row's generated_at >= second row's
    assert rows[0]["generated_at"] >= rows[1]["generated_at"]


async def test_endpoint_get_flips_draft_to_sent(orchestrator_client):
    pid, _ = await _seed_patient()
    create = orchestrator_client.post(
        f"/patients/{pid}/visit-briefs/generate", json={}
    )
    brief_id = create.json()["id"]

    # First GET should flip the status.
    detail = orchestrator_client.get(f"/visit-briefs/{brief_id}")
    assert detail.status_code == 200
    assert detail.json()["status"] == "sent"

    # Second GET stays "sent" (idempotent for non-draft).
    detail2 = orchestrator_client.get(f"/visit-briefs/{brief_id}")
    assert detail2.status_code == 200
    assert detail2.json()["status"] == "sent"


async def test_endpoint_get_404_for_missing_brief(orchestrator_client):
    response = orchestrator_client.get("/visit-briefs/999999999")
    assert response.status_code == 404


async def test_endpoint_list_excludes_failed_by_default(
    monkeypatch, orchestrator_client
):
    """Failed briefs land in the table but the default list
    response hides them — only ``include_failed=true`` surfaces
    them for ops debugging."""
    pid, _ = await _seed_patient()

    # Successful brief (LLM disabled placeholder counts as
    # successful since error is None).
    orchestrator_client.post(
        f"/patients/{pid}/visit-briefs/generate", json={}
    )

    # Force a failed brief.
    fake = _FakeLLM(
        parsed={"summary": "", "talking_points": [], "red_flags": []},
        raise_exc=RuntimeError("forced"),
    )
    monkeypatch.setattr(
        visit_brief_generator, "get_llm", lambda: fake
    )
    fail_resp = orchestrator_client.post(
        f"/patients/{pid}/visit-briefs/generate", json={}
    )
    assert fail_resp.status_code == 502

    monkeypatch.undo()

    default_list = orchestrator_client.get(
        f"/patients/{pid}/visit-briefs"
    ).json()
    full_list = orchestrator_client.get(
        f"/patients/{pid}/visit-briefs?include_failed=true"
    ).json()

    default_errors = [b for b in default_list if b["error"]]
    full_errors = [b for b in full_list if b["error"]]
    assert default_errors == []
    assert len(full_errors) >= 1

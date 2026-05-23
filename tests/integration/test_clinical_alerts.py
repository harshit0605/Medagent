"""Integration tests for clinical-alert detection + endpoints.

End-to-end against real Postgres because:
    1. The triage classifier persists rows via the orchestrator
       /route path; correctness depends on the joined inbound +
       classification + alert insert sequence.
    2. The ack/resolve workflow relies on Postgres' transaction
       semantics for the timestamp + actor stamping.
    3. The patient detail sub-list joins back via FK — exactly
       the kind of relationship we want exercised in
       integration.

LLM behaviour: tests with the default ``LLM_ENABLED=0`` exercise
the classifier's "(LLM disabled — triage skipped)" path, which
deliberately returns severity="none" so no alert is created.
A separate test stubs ``get_llm`` to verify the alert path
fires only when severity ∈ {high, critical}.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.db.models import ClinicalAlert, Patient
from app.db.repositories import (
    clinical_alerts as clinical_alerts_repo,
)
from app.db.session import get_sessionmaker
from services.orchestrator import triage_classifier

pytestmark = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"),
    reason="DATABASE_URL not set — skipping clinical-alert tests",
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
            full_name=f"Alert Test {suffix}",
            phone=f"alert-{suffix}",
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
    severity: str = "high",
    summary: str = "Test alert summary",
    inbound: str = "test inbound text",
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


# ---- repo / lifecycle tests ------------------------------------------------


async def test_create_rejects_unknown_severity():
    """Severity must be high or critical — alerts only fire
    for those tiers. The classifier itself can return medium /
    low / none, but the persistence path filters them out."""
    pid, phone = await _seed_patient()
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        with pytest.raises(ValueError, match="severity"):
            await clinical_alerts_repo.create(
                db,
                patient_id=pid,
                patient_phone=phone,
                message_id=None,
                severity="medium",
                red_flags=[],
                clinical_summary="should reject",
                inbound_text="x",
                llm_model=None,
            )


async def test_acknowledge_stamps_actor_and_time():
    pid, phone = await _seed_patient()
    alert_id = await _seed_alert(patient_id=pid, phone=phone)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        row = await clinical_alerts_repo.acknowledge(
            db, alert_id, actor="dr.smith"
        )
        await db.commit()

    assert row is not None
    assert row.status == "acknowledged"
    assert row.acknowledged_by == "dr.smith"
    assert row.acknowledged_at is not None


async def test_acknowledge_idempotent_after_first_call():
    """Double-clicking the ack button shouldn't change the
    actor or timestamp on the second click."""
    pid, phone = await _seed_patient()
    alert_id = await _seed_alert(patient_id=pid, phone=phone)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        first = await clinical_alerts_repo.acknowledge(
            db, alert_id, actor="dr.first"
        )
        await db.commit()

    async with SessionLocal() as db:
        second = await clinical_alerts_repo.acknowledge(
            db, alert_id, actor="dr.second"
        )
        await db.commit()

    assert first is not None
    assert second is not None
    assert second.acknowledged_by == "dr.first"
    assert second.acknowledged_at == first.acknowledged_at


async def test_resolve_from_open_stamps_both_ack_and_resolve():
    """A doctor can resolve directly without intermediate ack
    (e.g. they recognised it as false positive). Resolution
    stamps acknowledged_at too so the audit trail isn't blank."""
    pid, phone = await _seed_patient()
    alert_id = await _seed_alert(patient_id=pid, phone=phone)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        row = await clinical_alerts_repo.resolve(
            db,
            alert_id,
            actor="dr.skip-ack",
            notes="false positive — patient was joking",
        )
        await db.commit()

    assert row is not None
    assert row.status == "resolved"
    assert row.acknowledged_at is not None
    assert row.acknowledged_by == "dr.skip-ack"
    assert row.resolved_at is not None
    assert row.resolved_by == "dr.skip-ack"
    assert "false positive" in (row.resolution_notes or "")


async def test_resolve_from_acknowledged_keeps_original_ack_actor():
    """Ack first, then resolve — the resolver's name shouldn't
    overwrite the acknowledger's stamp."""
    pid, phone = await _seed_patient()
    alert_id = await _seed_alert(patient_id=pid, phone=phone)

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await clinical_alerts_repo.acknowledge(
            db, alert_id, actor="nurse.triage"
        )
        await db.commit()

    async with SessionLocal() as db:
        row = await clinical_alerts_repo.resolve(
            db, alert_id, actor="dr.handler", notes=None
        )
        await db.commit()

    assert row is not None
    assert row.acknowledged_by == "nurse.triage"
    assert row.resolved_by == "dr.handler"


async def test_count_by_status_returns_grouped_counters():
    pid, phone = await _seed_patient()
    open_id = await _seed_alert(patient_id=pid, phone=phone)
    await _seed_alert(
        patient_id=pid, phone=phone, severity="critical"
    )
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await clinical_alerts_repo.resolve(
            db, open_id, actor="ops", notes=None
        )
        await db.commit()

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        counts = await clinical_alerts_repo.count_by_status(db)

    assert counts.get("open", 0) >= 1
    assert counts.get("resolved", 0) >= 1


# ---- triage classifier tests -----------------------------------------------


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


async def test_classifier_returns_none_when_llm_disabled():
    """Default test env has LLM_ENABLED=0 — the classifier
    must return severity=none so the alert path doesn't fire."""
    decision = await triage_classifier.classify_clinical_urgency(
        text="I have severe chest pain", intent="clinical_question"
    )
    assert decision.severity == "none"


async def test_classifier_returns_critical_for_red_flag(monkeypatch):
    """Stub the LLM to return critical — the classifier
    forwards severity + red_flags + summary intact."""
    fake = _FakeLLM(
        parsed={
            "severity": "critical",
            "red_flags": ["chest pain", "shortness of breath"],
            "summary": "Patient reports chest pain at rest",
        }
    )
    monkeypatch.setattr(triage_classifier, "get_llm", lambda: fake)

    decision = await triage_classifier.classify_clinical_urgency(
        text="severe chest pain right now, can't breathe",
        intent="clinical_question",
    )
    assert decision.severity == "critical"
    assert "chest pain" in decision.red_flags
    assert "chest pain" in decision.summary.lower()


async def test_classifier_returns_none_for_empty_text():
    decision = await triage_classifier.classify_clinical_urgency(
        text="   ", intent=None
    )
    assert decision.severity == "none"


async def test_alert_severities_constant_excludes_low_tiers():
    """ALERT_SEVERITIES governs whether an alert row is
    written. Low / medium / none must not be in the set —
    otherwise the inbox would flood with false positives."""
    assert "high" in triage_classifier.ALERT_SEVERITIES
    assert "critical" in triage_classifier.ALERT_SEVERITIES
    assert "medium" not in triage_classifier.ALERT_SEVERITIES
    assert "low" not in triage_classifier.ALERT_SEVERITIES
    assert "none" not in triage_classifier.ALERT_SEVERITIES


async def test_classifier_kill_switch_short_circuits(monkeypatch):
    """``TRIAGE_ENABLED=0`` must short-circuit the classifier
    BEFORE the LLM is called — even if the LLM is otherwise
    healthy. Lets ops flip an env var to drop triage cost
    without restarting or disabling all LLM features."""
    fake = _FakeLLM(
        parsed={
            "severity": "critical",
            "red_flags": ["chest pain"],
            "summary": "would have raised an alert",
        }
    )
    monkeypatch.setattr(triage_classifier, "get_llm", lambda: fake)
    monkeypatch.setenv("TRIAGE_ENABLED", "0")

    decision = await triage_classifier.classify_clinical_urgency(
        text="severe chest pain right now",
        intent="clinical_question",
    )
    assert decision.severity == "none"
    assert "TRIAGE_ENABLED=0" in decision.summary


async def test_classifier_kill_switch_default_on(monkeypatch):
    """Absent env var → triage runs (default ON). Belt-and-
    suspenders: confirm the flag-check doesn't break the
    happy path."""
    fake = _FakeLLM(
        parsed={
            "severity": "high",
            "red_flags": ["worsening symptoms"],
            "summary": "high severity",
        }
    )
    monkeypatch.setattr(triage_classifier, "get_llm", lambda: fake)
    monkeypatch.delenv("TRIAGE_ENABLED", raising=False)

    decision = await triage_classifier.classify_clinical_urgency(
        text="rash spreading badly", intent="clinical_question"
    )
    assert decision.severity == "high"


# ---- endpoint tests --------------------------------------------------------


async def test_endpoint_list_returns_alerts(orchestrator_client):
    pid, phone = await _seed_patient()
    await _seed_alert(patient_id=pid, phone=phone)

    response = orchestrator_client.get("/clinical-alerts?limit=200")
    assert response.status_code == 200
    rows = response.json()
    assert any(a["patient_id"] == pid for a in rows)


async def test_endpoint_list_filters_by_status(orchestrator_client):
    pid, phone = await _seed_patient()
    open_id = await _seed_alert(patient_id=pid, phone=phone)
    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        await clinical_alerts_repo.resolve(
            db, open_id, actor="ops", notes=None
        )
        await db.commit()

    resp_resolved = orchestrator_client.get(
        "/clinical-alerts?status=resolved&limit=500"
    ).json()
    resp_open = orchestrator_client.get(
        "/clinical-alerts?status=open&limit=500"
    ).json()
    assert any(a["id"] == open_id for a in resp_resolved)
    assert not any(a["id"] == open_id for a in resp_open)


async def test_endpoint_list_400_for_bad_status(orchestrator_client):
    response = orchestrator_client.get(
        "/clinical-alerts?status=bogus"
    )
    assert response.status_code == 400


async def test_endpoint_get_404_for_missing(orchestrator_client):
    response = orchestrator_client.get(
        "/clinical-alerts/999999999"
    )
    assert response.status_code == 404


async def test_endpoint_acknowledge_round_trip(orchestrator_client):
    pid, phone = await _seed_patient()
    alert_id = await _seed_alert(patient_id=pid, phone=phone)

    response = orchestrator_client.post(
        f"/clinical-alerts/{alert_id}/acknowledge",
        json={"actor": "dr.endpoint"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "acknowledged"
    assert body["acknowledged_by"] == "dr.endpoint"


async def test_endpoint_resolve_with_notes(orchestrator_client):
    pid, phone = await _seed_patient()
    alert_id = await _seed_alert(patient_id=pid, phone=phone)

    response = orchestrator_client.post(
        f"/clinical-alerts/{alert_id}/resolve",
        json={
            "actor": "dr.resolver",
            "notes": "called patient, symptoms improving",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "resolved"
    assert "improving" in (body["resolution_notes"] or "")


async def test_endpoint_counts_returns_zero_filled_keys(
    orchestrator_client,
):
    response = orchestrator_client.get("/clinical-alerts/counts")
    assert response.status_code == 200
    counts = response.json()
    # All three keys present even if no rows.
    assert set(counts.keys()) >= {"open", "acknowledged", "resolved"}
    for v in counts.values():
        assert isinstance(v, int)


async def test_endpoint_per_patient_list(orchestrator_client):
    pid, phone = await _seed_patient()
    other_pid, other_phone = await _seed_patient()
    own = await _seed_alert(patient_id=pid, phone=phone)
    other = await _seed_alert(
        patient_id=other_pid, phone=other_phone
    )

    response = orchestrator_client.get(
        f"/patients/{pid}/clinical-alerts"
    )
    assert response.status_code == 200
    rows = response.json()
    ids = {a["id"] for a in rows}
    assert own in ids
    assert other not in ids


async def test_classifier_creates_alert_via_route_endpoint(
    monkeypatch, orchestrator_client
):
    """Full /route → triage → alert flow. Stub the LLM to
    return critical severity; verify a clinical_alerts row
    lands keyed to the patient."""
    pid, phone = await _seed_patient()

    fake = _FakeLLM(
        parsed={
            "severity": "critical",
            "red_flags": ["chest pain", "shortness of breath"],
            "summary": "Patient reports chest pain at rest with breathing difficulty",
        }
    )
    monkeypatch.setattr(triage_classifier, "get_llm", lambda: fake)

    response = orchestrator_client.post(
        "/route",
        json={
            "message": {
                "patient_id": phone,
                "text": "severe chest pain right now and can't catch my breath",
                "input_kind": "text",
                "message_id": f"wamid-{uuid.uuid4().hex[:8]}",
            },
        },
    )
    assert response.status_code == 200

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        from sqlalchemy import select

        rows = list(
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
    assert len(rows) >= 1
    latest = rows[-1]
    assert latest.severity == "critical"
    assert "chest pain" in latest.red_flags
    assert latest.message_id is not None  # FK to message_log


async def test_classifier_does_not_create_alert_for_low_severity(
    monkeypatch, orchestrator_client
):
    """Routine messages must NOT generate alert rows even
    when the classifier sees them. Otherwise the queue floods
    and doctors lose trust in the signal."""
    pid, phone = await _seed_patient()

    fake = _FakeLLM(
        parsed={
            "severity": "low",
            "red_flags": [],
            "summary": "(routine update)",
        }
    )
    monkeypatch.setattr(triage_classifier, "get_llm", lambda: fake)

    response = orchestrator_client.post(
        "/route",
        json={
            "patient_id": phone,
            "message": {
                "text": "thanks doc, I'll see you next week",
                "input_kind": "text",
                "message_id": f"wamid-{uuid.uuid4().hex[:8]}",
            },
        },
    )
    assert response.status_code == 200

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        from sqlalchemy import select

        rows = list(
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
    assert rows == []

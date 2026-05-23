"""Unit tests for the orchestrator prescription handler.

Covers marker detection, route short-circuit, vision-LLM success + failure
paths, and the patient ack copy. DB session is mocked.
"""

from __future__ import annotations

import types
from datetime import datetime, timezone

import pytest

from services.orchestrator import prescription_handler as h


class _NoopAsyncSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None

    async def commit(self):
        return None

    async def get(self, *_a, **_k):
        return None


def _patch_session(monkeypatch):
    def factory():
        return _NoopAsyncSession()

    monkeypatch.setattr(h, "get_sessionmaker", lambda: factory)


def _patient(*, id=1, phone="9100", name="Asha"):
    return types.SimpleNamespace(id=id, phone=phone, full_name=name)


def _stub_repos(monkeypatch, *, patient, llm_result=None):
    """Patch the repos prescription_handler reaches for. Returns a captured-
    calls dict for assertions."""
    captured: dict = {"created": [], "updated": []}

    async def get_by_phone(_db, _phone):
        return patient

    async def create_prescription(_db, **kwargs):
        prescription = types.SimpleNamespace(
            id=42,
            patient_id=kwargs["patient_id"],
            source_upload_url=kwargs["source_upload_url"],
            parsed_payload=kwargs.get("parsed_payload") or {},
            human_verification_status=types.SimpleNamespace(value="pending"),
        )
        captured["created"].append(kwargs)
        return prescription

    async def set_parsed_payload(_db, _id, **kwargs):
        captured["updated"].append((_id, kwargs))
        return None

    monkeypatch.setattr(h.patients_repo, "get_by_phone", get_by_phone)
    monkeypatch.setattr(h.prescriptions_repo, "create", create_prescription)
    monkeypatch.setattr(
        h.prescriptions_repo, "set_parsed_payload", set_parsed_payload
    )

    fake_llm = types.SimpleNamespace()

    async def parse_prescription_image(*, image_url):
        captured["vision_url"] = image_url
        return llm_result

    fake_llm.parse_prescription_image = parse_prescription_image
    monkeypatch.setattr(h, "get_llm", lambda: fake_llm)
    return captured


def _vision_result(**overrides):
    """Build a ParsePrescriptionResult-shaped namespace."""
    parsed = types.SimpleNamespace(
        model_dump=lambda: {
            "confidence": overrides.get("confidence", "high"),
            "regimens": overrides.get(
                "regimens",
                [
                    {
                        "medication_name": "Metformin",
                        "dose": "500 mg",
                        "times_of_day": ["08:00", "20:00"],
                        "frequency_text": "twice daily",
                        "duration_days": None,
                        "notes": None,
                    }
                ],
            ),
            "summary": overrides.get("summary", "1 medication, clear dosing"),
            "illegible": overrides.get("illegible", False),
        }
    )
    return types.SimpleNamespace(
        parsed=parsed,
        used_model=overrides.get("used_model", "test-vision"),
    )


def test_marker_detection_recognises_well_formed_inputs():
    assert h.looks_like_prescription_upload(
        "[prescription-upload] public_path=/uploads/abc.jpg mime=image/jpeg"
    )
    # Caption is optional.
    assert h.looks_like_prescription_upload(
        "[prescription-upload] public_path=/u/x.png"
    )
    # Caption with quotes.
    assert h.looks_like_prescription_upload(
        '[prescription-upload] public_path=/u/y.jpg mime=image/jpeg caption="from clinic"'
    )
    assert not h.looks_like_prescription_upload("a regular text message")
    assert not h.looks_like_prescription_upload("")


async def test_handle_unknown_patient_replies_gracefully(monkeypatch):
    captured = _stub_repos(monkeypatch, patient=None)
    _patch_session(monkeypatch)
    delta = await h.handle_prescription_upload(
        patient_phone="9999",
        new_user_text="[prescription-upload] public_path=/u/x.jpg",
    )
    assert delta is not None
    assert "couldn't find your account" in delta["response_body"].lower()
    assert delta["audit_reasons"] == ["prescription_unknown_patient"]
    # No row created.
    assert captured["created"] == []


async def test_handle_creates_row_and_acks_when_vision_succeeds(monkeypatch):
    captured = _stub_repos(
        monkeypatch, patient=_patient(), llm_result=_vision_result()
    )
    _patch_session(monkeypatch)
    delta = await h.handle_prescription_upload(
        patient_phone="9100",
        new_user_text="[prescription-upload] public_path=/uploads/rx.jpg mime=image/jpeg",
    )
    assert delta is not None
    assert delta["audit_reasons"] == ["prescription_received_parsed"]
    assert "Metformin" in delta["response_body"]
    # Row created + parsed_payload was set with the structured result.
    assert len(captured["created"]) == 1
    payload_updates = [u[1]["parsed_payload"] for u in captured["updated"]]
    final_payload = payload_updates[-1]
    assert final_payload["vision_parse_attempted"] is True
    assert final_payload["vision_parse_failed"] is False
    assert final_payload["parsed"]["regimens"][0]["medication_name"] == "Metformin"
    # Vision URL composed of public base + path.
    assert captured["vision_url"].endswith("/uploads/rx.jpg")


async def test_handle_falls_back_when_vision_fails(monkeypatch):
    captured = _stub_repos(
        monkeypatch, patient=_patient(), llm_result=None
    )
    _patch_session(monkeypatch)
    delta = await h.handle_prescription_upload(
        patient_phone="9100",
        new_user_text="[prescription-upload] public_path=/uploads/blurry.jpg",
    )
    assert delta is not None
    assert delta["audit_reasons"] == ["prescription_received_no_parse"]
    assert "manually" in delta["response_body"].lower()
    # Row was still created so the clinician can enter regimens manually.
    assert len(captured["created"]) == 1
    payload = captured["updated"][-1][1]["parsed_payload"]
    assert payload["vision_parse_attempted"] is True
    assert payload["vision_parse_failed"] is True


async def test_handle_illegible_image_uses_special_copy(monkeypatch):
    captured = _stub_repos(
        monkeypatch,
        patient=_patient(),
        llm_result=_vision_result(illegible=True, regimens=[]),
    )
    _patch_session(monkeypatch)
    delta = await h.handle_prescription_upload(
        patient_phone="9100",
        new_user_text="[prescription-upload] public_path=/uploads/x.jpg",
    )
    assert delta is not None
    assert delta["audit_reasons"] == ["prescription_received_illegible"]
    assert "hard to read" in delta["response_body"].lower()


async def test_handle_returns_none_for_non_marker_text(monkeypatch):
    _patch_session(monkeypatch)
    out = await h.handle_prescription_upload(
        patient_phone="9100",
        new_user_text="hi can you book me an appointment?",
    )
    assert out is None

"""Unit tests for the async scheduler dispatcher (no DB, no real HTTP)."""

from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.db.models import AppointmentStatus
from services.scheduler import dispatcher as dispatcher_module


@pytest.fixture(autouse=True)
def _stub_consent_gate(monkeypatch):
    """Default the dispatcher's opt-out consent gate to "consenting"
    for every test in this file. The gate calls
    ``patients_repo.get_by_phone(db, phone)`` which the existing tests
    don't expect to fire (they pass ``db=object()`` as a placeholder).

    Tests that specifically exercise the gate live in
    tests/test_dispatcher_optout_gate.py and override this fixture by
    monkey-patching ``get_by_phone`` themselves before calling
    dispatch."""

    async def _consenting(_db, phone):
        return types.SimpleNamespace(id=1, phone=phone, consent_sms=True)

    monkeypatch.setattr(
        dispatcher_module.patients_repo, "get_by_phone", _consenting
    )


def _fake_event(
    *,
    event_type: str,
    payload: dict,
    scheduled_for: datetime | None = None,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=42,
        event_type=event_type,
        patient_id="p-test",
        scheduled_for=scheduled_for or datetime.now(timezone.utc),
        payload=payload,
    )


class _MockResponse:
    def raise_for_status(self) -> None:
        return None


class _MockAsyncClient:
    """Minimal stand-in for httpx.AsyncClient as a context manager."""

    def __init__(self, *, post=None, _captured: dict | None = None) -> None:
        self._post = post or (lambda url, json: _MockResponse())
        self._captured = _captured if _captured is not None else {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def post(self, url, json):
        self._captured["url"] = url
        self._captured["json"] = json
        return self._post(url, json)


def _patch_async_client(monkeypatch, post=None) -> dict:
    captured: dict = {}

    def factory(*_args, **_kwargs):
        return _MockAsyncClient(post=post, _captured=captured)

    monkeypatch.setattr(dispatcher_module.httpx, "AsyncClient", factory)
    return captured


async def test_dispatch_maps_triage_alert_to_template(monkeypatch):
    """Generic template-path dispatch — covers any event_type still in the
    legacy template-by-event mapping (dose_due / refill_due now have their
    own dispatcher branches and aren't tested here)."""
    captured = _patch_async_client(monkeypatch)
    err = await dispatcher_module.dispatch(
        _fake_event(
            event_type="triage_alert",
            payload={
                "cohort": "diabetes",
                "severity": "high",
                "reason": "hypo episode reported",
            },
        ),
        gateway_url="http://gw:1",
    )
    assert err is None
    assert captured["url"] == "http://gw:1/send"
    assert captured["json"]["template_name"] == "triage_alert_v1"
    assert captured["json"]["use_template"] is True
    assert captured["json"]["template_params"]["severity"] == "high"


async def test_dispatch_picks_lab_vs_appointment_template(monkeypatch):
    seen: list[str] = []

    def post(url, json):
        seen.append(json["template_name"])
        return _MockResponse()

    _patch_async_client(monkeypatch, post=post)

    await dispatcher_module.dispatch(
        _fake_event(
            event_type="followup_closure",
            payload={"followup_type": "lab", "item_name": "HbA1c", "status": "completed"},
        ),
        gateway_url="http://gw:1",
    )
    await dispatcher_module.dispatch(
        _fake_event(
            event_type="followup_closure",
            payload={"followup_type": "appointment", "item_name": "Dr X", "status": "completed"},
        ),
        gateway_url="http://gw:1",
    )
    assert seen == ["lab_closure_update_v1", "appointment_closure_update_v1"]


async def test_dispatch_returns_unmapped_for_unknown_event_type(monkeypatch):
    def must_not_call(url, json):  # noqa: ARG001
        raise AssertionError("must not call HTTP for unmapped events")

    _patch_async_client(monkeypatch, post=must_not_call)

    err = await dispatcher_module.dispatch(
        _fake_event(event_type="something_weird", payload={}),
        gateway_url="http://gw:1",
    )
    assert err is not None
    assert err.startswith("unmapped:")


async def test_dispatch_returns_http_error_string_on_failure(monkeypatch):
    def boom(url, json):  # noqa: ARG001
        raise httpx.ConnectError("refused")

    _patch_async_client(monkeypatch, post=boom)

    err = await dispatcher_module.dispatch(
        _fake_event(
            event_type="triage_alert",
            payload={
                "cohort": "diabetes",
                "severity": "high",
                "reason": "broken pump",
            },
        ),
        gateway_url="http://gw:1",
    )
    assert err is not None
    assert err.startswith("http_error:")


async def test_dispatch_stringifies_payload_params(monkeypatch):
    captured = _patch_async_client(monkeypatch)
    await dispatcher_module.dispatch(
        _fake_event(
            event_type="triage_alert",
            payload={"cohort": "diabetes", "severity": "high", "reason": "spike"},
        ),
        gateway_url="http://gw:1",
    )
    params = captured["json"]["template_params"]
    assert params["severity"] == "high"
    assert params["reason"] == "spike"


# ---- Appointment reminder dispatch ------------------------------------------


class _StubDB:
    """Async-context-manager stand-in for AsyncSession that returns canned rows."""

    def __init__(self, *, appointment, doctor) -> None:
        self._appt = appointment
        self._doctor = doctor


async def _stub_get_appointment(_db, appt_id):  # noqa: ARG001
    return _CURRENT_APPT


async def _stub_get_doctor(_db, doctor_id):  # noqa: ARG001
    return _CURRENT_DOCTOR


async def _stub_get_patient(_db, patient_id):  # noqa: ARG001
    return _CURRENT_PATIENT


_CURRENT_APPT = None
_CURRENT_DOCTOR = None
_CURRENT_PATIENT = None


def _set_stub_state(monkeypatch, *, appointment, doctor, patient=None):
    global _CURRENT_APPT, _CURRENT_DOCTOR, _CURRENT_PATIENT
    _CURRENT_APPT = appointment
    _CURRENT_DOCTOR = doctor
    _CURRENT_PATIENT = patient
    monkeypatch.setattr(
        dispatcher_module.appointments_repo, "get", _stub_get_appointment
    )
    monkeypatch.setattr(dispatcher_module.doctors_repo, "get", _stub_get_doctor)
    monkeypatch.setattr(
        dispatcher_module.patients_repo, "get", _stub_get_patient
    )


def _set_csw(monkeypatch, *, in_csw: bool):
    """Stub patient_inbound_repo.get_last_inbound to control CSW status."""
    if in_csw:
        ts = datetime.now(timezone.utc) - timedelta(hours=1)
    else:
        # 3 days ago — well outside the 24h window.
        ts = datetime.now(timezone.utc) - timedelta(hours=72)

    async def _stub_get_last_inbound(_db, _patient_id):
        return ts

    monkeypatch.setattr(
        dispatcher_module.patient_inbound_repo,
        "get_last_inbound",
        _stub_get_last_inbound,
    )


def _confirmed_appt():
    return types.SimpleNamespace(
        id=99,
        status=AppointmentStatus.confirmed,
    )


def _doc(name="Dr Harshit", tz="Asia/Kolkata"):
    return types.SimpleNamespace(id=1, name=name, timezone=tz)


async def test_dispatch_appointment_reminder_24h_renders_freeform(monkeypatch):
    captured = _patch_async_client(monkeypatch)
    _set_stub_state(monkeypatch, appointment=_confirmed_appt(), doctor=_doc())
    # In-CSW: simulate the patient messaged us 1h ago.
    _set_csw(monkeypatch, in_csw=True)

    appt_start = datetime.now(timezone.utc) + timedelta(hours=24)
    err = await dispatcher_module.dispatch(
        _fake_event(
            event_type="appointment_reminder_24h",
            payload={
                "appointment_id": 99,
                "doctor_id": 1,
                "appointment_start_iso": appt_start.isoformat(),
            },
        ),
        db=object(),
        gateway_url="http://gw:1",
    )
    assert err is None
    body = captured["json"]
    assert body["use_template"] is False
    assert "Dr Harshit" in body["body"]
    assert "tomorrow" in body["body"].lower()
    # Reminder ships with two interactive reply buttons referencing appt id.
    button_labels = [b["label"] for b in body["buttons"]]
    assert button_labels == ["Cancel appointment", "Reschedule"]
    assert all("99" in b["id"] for b in body["buttons"])


async def test_dispatch_appointment_reminder_uses_template_outside_csw(monkeypatch):
    captured = _patch_async_client(monkeypatch)
    _set_stub_state(
        monkeypatch,
        appointment=_confirmed_appt(),
        doctor=_doc(),
        patient=types.SimpleNamespace(id=42, full_name="Asha Kumar"),
    )
    # Out-of-CSW: patient hasn't messaged in days.
    _set_csw(monkeypatch, in_csw=False)

    appt_start = datetime.now(timezone.utc) + timedelta(hours=24)
    err = await dispatcher_module.dispatch(
        _fake_event(
            event_type="appointment_reminder_24h",
            payload={
                "appointment_id": 99,
                "doctor_id": 1,
                "patient_db_id": 42,
                "appointment_start_iso": appt_start.isoformat(),
            },
        ),
        db=object(),
        gateway_url="http://gw:1",
    )
    assert err is None
    body = captured["json"]
    # Template send: use_template=True, our custom appointment_reminder_v1
    # (in `en`, with quick-reply buttons), 2 params (doctor + scheduled
    # time). Body is included for log/debug parity with the freeform path.
    assert body["use_template"] is True
    assert body["template_name"] == "appointment_reminder_v1"
    params = body["template_params"]
    # Lock the {{1}} → {{2}} order via key prefixes so meta.py's
    # sorted-key emission yields doctor first, time second.
    assert sorted(params.keys()) == ["1_doctor", "2_when"]
    assert params["1_doctor"] == "Dr Harshit"
    assert params["2_when"]
    # appointment_reminder_v1 has 2 quick-reply buttons (Cancel /
    # Reschedule) — dispatcher injects the dynamic appointment-id
    # payloads at indexes 0/1 so the patient's tap lands back in the
    # booking-agent path.
    assert len(body["buttons"]) == 2
    button_ids = {b["id"] for b in body["buttons"]}
    assert button_ids == {"cancel_appt:99", "reschedule_appt:99"}


async def test_dispatch_appointment_reminder_1h_uses_one_hour_phrasing(monkeypatch):
    captured = _patch_async_client(monkeypatch)
    _set_stub_state(monkeypatch, appointment=_confirmed_appt(), doctor=_doc())
    _set_csw(monkeypatch, in_csw=True)

    appt_start = datetime.now(timezone.utc) + timedelta(hours=1)
    err = await dispatcher_module.dispatch(
        _fake_event(
            event_type="appointment_reminder_1h",
            payload={
                "appointment_id": 99,
                "doctor_id": 1,
                "appointment_start_iso": appt_start.isoformat(),
            },
        ),
        db=object(),
        gateway_url="http://gw:1",
    )
    assert err is None
    assert "1 hour" in captured["json"]["body"]


async def test_dispatch_skips_stale_one_hour_reminder(monkeypatch):
    def must_not_call(url, json):  # noqa: ARG001
        raise AssertionError("stale event must not hit gateway")

    _patch_async_client(monkeypatch, post=must_not_call)
    _set_stub_state(monkeypatch, appointment=_confirmed_appt(), doctor=_doc())

    # Scheduled 45 min ago — beyond the 30-min freshness window for 1h reminders.
    err = await dispatcher_module.dispatch(
        _fake_event(
            event_type="appointment_reminder_1h",
            payload={
                "appointment_id": 99,
                "doctor_id": 1,
                "appointment_start_iso": datetime.now(timezone.utc).isoformat(),
            },
            scheduled_for=datetime.now(timezone.utc) - timedelta(minutes=45),
        ),
        db=object(),
        gateway_url="http://gw:1",
    )
    assert err is not None
    assert err.startswith("stale:")


async def test_dispatch_skips_when_appointment_cancelled(monkeypatch):
    def must_not_call(url, json):  # noqa: ARG001
        raise AssertionError("not_applicable event must not hit gateway")

    _patch_async_client(monkeypatch, post=must_not_call)
    cancelled_appt = types.SimpleNamespace(id=99, status=AppointmentStatus.cancelled)
    _set_stub_state(monkeypatch, appointment=cancelled_appt, doctor=_doc())
    _set_csw(monkeypatch, in_csw=True)

    err = await dispatcher_module.dispatch(
        _fake_event(
            event_type="appointment_reminder_24h",
            payload={
                "appointment_id": 99,
                "doctor_id": 1,
                "appointment_start_iso": (
                    datetime.now(timezone.utc) + timedelta(hours=24)
                ).isoformat(),
            },
        ),
        db=object(),
        gateway_url="http://gw:1",
    )
    assert err is not None
    assert err.startswith("not_applicable:")


async def test_dispatch_appointment_reminder_requires_db(monkeypatch):
    def must_not_call(url, json):  # noqa: ARG001
        raise AssertionError("must not hit gateway when db missing")

    _patch_async_client(monkeypatch, post=must_not_call)

    err = await dispatcher_module.dispatch(
        _fake_event(
            event_type="appointment_reminder_24h",
            payload={
                "appointment_id": 99,
                "doctor_id": 1,
                "appointment_start_iso": (
                    datetime.now(timezone.utc) + timedelta(hours=24)
                ).isoformat(),
            },
        ),
        db=None,
        gateway_url="http://gw:1",
    )
    assert err is not None
    assert err.startswith("unmapped:")


# ---- Dose reminder dispatch -------------------------------------------------


from app.db.models import AdherenceStatus  # noqa: E402


def _scheduled_adherence(adherence_id: int = 7):
    return types.SimpleNamespace(
        id=adherence_id,
        status=AdherenceStatus.scheduled,
        scheduled_at=datetime.now(timezone.utc),
        regimen_id=1,
        patient_id=2,
    )


async def _stub_get_adherence(_db, adherence_id):  # noqa: ARG001
    return _CURRENT_ADHERENCE


_CURRENT_ADHERENCE = None


def _set_adherence(monkeypatch, *, adherence):
    global _CURRENT_ADHERENCE
    _CURRENT_ADHERENCE = adherence
    # The dispatcher imports adherence_events_repo lazily inside the
    # _build_dose_reminder function, so patch it on its module path.
    from app.db.repositories import adherence_events as adherence_events_repo

    monkeypatch.setattr(adherence_events_repo, "get", _stub_get_adherence)


async def test_dispatch_dose_due_renders_freeform_buttons_in_csw(monkeypatch):
    captured = _patch_async_client(monkeypatch)
    _set_csw(monkeypatch, in_csw=True)
    _set_adherence(monkeypatch, adherence=_scheduled_adherence(adherence_id=42))

    err = await dispatcher_module.dispatch(
        _fake_event(
            event_type="dose_due",
            payload={
                "adherence_event_id": 42,
                "regimen_id": 1,
                "patient_db_id": 2,
                "medication_name": "Metformin",
                "dose": "500 mg",
                "scheduled_at_iso": datetime.now(timezone.utc).isoformat(),
            },
        ),
        db=object(),
        gateway_url="http://gw:1",
    )
    assert err is None
    body = captured["json"]
    assert body["use_template"] is False
    assert "Metformin" in body["body"]
    assert "500 mg" in body["body"]
    button_labels = [b["label"] for b in body["buttons"]]
    assert button_labels == ["Taken", "Snooze 30m", "Skipped"]
    assert all("42" in b["id"] for b in body["buttons"])


async def test_dispatch_dose_due_uses_template_outside_csw(monkeypatch):
    captured = _patch_async_client(monkeypatch)
    _set_csw(monkeypatch, in_csw=False)
    _set_adherence(monkeypatch, adherence=_scheduled_adherence(adherence_id=42))

    err = await dispatcher_module.dispatch(
        _fake_event(
            event_type="dose_due",
            payload={
                "adherence_event_id": 42,
                "regimen_id": 1,
                "patient_db_id": 2,
                "medication_name": "Metformin",
                "dose": "500 mg",
                "scheduled_at_iso": datetime.now(timezone.utc).isoformat(),
            },
        ),
        db=object(),
        gateway_url="http://gw:1",
    )
    assert err is None
    body = captured["json"]
    assert body["use_template"] is True
    # Default is v1 (APPROVED at Meta, no button components). Once v2
    # approves, ops flips ``WHATSAPP_DOSE_TEMPLATE_NAME=dose_reminder_v2``
    # and the dispatcher's _v2-suffix gate auto-injects dose_taken /
    # dose_skipped button payloads. See test_v2_template_injects_buttons
    # for that path.
    assert body["template_name"] == "dose_reminder_v1"
    assert "Metformin" in body["template_params"]["1_med"]
    # v1 has no button components — dispatcher must NOT attach buttons.
    assert "buttons" not in body or body["buttons"] == []


async def test_dispatch_dose_due_v2_template_injects_buttons(monkeypatch):
    """When the env override flips to v2, the dispatcher attaches
    dynamic dose_taken / dose_skipped quick-reply payloads. Verifies
    the _v2-suffix gate without depending on the default value."""
    captured = _patch_async_client(monkeypatch)
    _set_csw(monkeypatch, in_csw=False)
    _set_adherence(monkeypatch, adherence=_scheduled_adherence(adherence_id=42))
    monkeypatch.setattr(dispatcher_module, "_DOSE_TEMPLATE_NAME", "dose_reminder_v2")

    err = await dispatcher_module.dispatch(
        _fake_event(
            event_type="dose_due",
            payload={
                "adherence_event_id": 42,
                "regimen_id": 1,
                "patient_db_id": 2,
                "medication_name": "Metformin",
                "dose": "500 mg",
                "scheduled_at_iso": datetime.now(timezone.utc).isoformat(),
            },
        ),
        db=object(),
        gateway_url="http://gw:1",
    )
    assert err is None
    body = captured["json"]
    assert body["template_name"] == "dose_reminder_v2"
    button_actions = {b["action"] for b in body.get("buttons", [])}
    assert button_actions == {"dose_taken", "dose_skipped"}


# ---- Refill reminder dispatch ------------------------------------------------


def _active_regimen_with_supply(*, supply_started_iso="2026-04-01"):
    from datetime import date as _date

    return types.SimpleNamespace(
        id=1,
        patient_id=2,
        medication_name="Metformin",
        dose="500 mg",
        ends_on=None,
        supply_days_initial=30,
        supply_started_on=_date.fromisoformat(supply_started_iso),
    )


_CURRENT_REGIMEN = None


async def _stub_get_regimen(_db, _id):  # noqa: ARG001
    return _CURRENT_REGIMEN


def _set_regimen(monkeypatch, regimen):
    global _CURRENT_REGIMEN
    _CURRENT_REGIMEN = regimen
    from app.db.repositories import regimens as regimens_repo

    monkeypatch.setattr(regimens_repo, "get", _stub_get_regimen)


async def test_dispatch_refill_due_renders_freeform_buttons_in_csw(monkeypatch):
    captured = _patch_async_client(monkeypatch)
    _set_csw(monkeypatch, in_csw=True)
    _set_regimen(monkeypatch, _active_regimen_with_supply())

    err = await dispatcher_module.dispatch(
        _fake_event(
            event_type="refill_due",
            payload={
                "regimen_id": 1,
                "patient_db_id": 2,
                "medication_name": "Metformin",
                "dose": "500 mg",
                "stage": "d3",
                "days_left": 3,
                "cycle_key": "2026-04-01",
            },
        ),
        db=object(),
        gateway_url="http://gw:1",
    )
    assert err is None
    body = captured["json"]
    assert body["use_template"] is False
    assert "Metformin" in body["body"]
    assert "3 day" in body["body"]
    button_labels = [b["label"] for b in body["buttons"]]
    assert button_labels == ["Refilled", "Snooze 1 day", "Need help"]
    assert all("1" in b["id"] for b in body["buttons"])


async def test_dispatch_refill_due_template_outside_csw(monkeypatch):
    captured = _patch_async_client(monkeypatch)
    _set_csw(monkeypatch, in_csw=False)
    _set_regimen(monkeypatch, _active_regimen_with_supply())

    err = await dispatcher_module.dispatch(
        _fake_event(
            event_type="refill_due",
            payload={
                "regimen_id": 1,
                "patient_db_id": 2,
                "medication_name": "Metformin",
                "dose": "500 mg",
                "stage": "d1",
                "days_left": 1,
                "cycle_key": "2026-04-01",
            },
        ),
        db=object(),
        gateway_url="http://gw:1",
    )
    assert err is None
    body = captured["json"]
    assert body["use_template"] is True
    assert body["template_name"] == "refill_due_v1"
    assert "Metformin" in body["template_params"]["1_med"]
    assert "buttons" not in body


async def test_dispatch_refill_skips_when_cycle_changed(monkeypatch):
    """If the patient already tapped Refilled, the regimen's cycle_key
    moves on. A pending refill_due event for the OLD cycle should be
    dropped rather than annoy the patient with a stale reminder."""
    def must_not_call(url, json):  # noqa: ARG001
        raise AssertionError("stale-cycle event must not hit gateway")

    _patch_async_client(monkeypatch, post=must_not_call)
    _set_csw(monkeypatch, in_csw=True)
    # Regimen now has a fresh cycle starting today.
    fresh = _active_regimen_with_supply(supply_started_iso="2026-05-02")
    _set_regimen(monkeypatch, fresh)

    err = await dispatcher_module.dispatch(
        _fake_event(
            event_type="refill_due",
            payload={
                "regimen_id": 1,
                "patient_db_id": 2,
                "medication_name": "Metformin",
                "dose": "500 mg",
                "stage": "d3",
                "days_left": 3,
                "cycle_key": "2026-04-01",  # OLD cycle — already refilled
            },
        ),
        db=object(),
        gateway_url="http://gw:1",
    )
    assert err is not None
    assert err.startswith("not_applicable:")


# ---- Lab follow-up dispatch -------------------------------------------------


from app.db.models import FollowupStatus  # noqa: E402


def _lab_followup(*, status=FollowupStatus.due, due_by=None):
    return types.SimpleNamespace(
        id=10,
        patient_id=2,
        test_name="HbA1c",
        status=status,
        due_by=due_by,
    )


_CURRENT_LAB = None


async def _stub_get_lab(_db, _id):  # noqa: ARG001
    return _CURRENT_LAB


def _set_lab(monkeypatch, lab):
    global _CURRENT_LAB
    _CURRENT_LAB = lab
    from app.db.repositories import lab_followups as lab_followups_repo

    monkeypatch.setattr(lab_followups_repo, "get", _stub_get_lab)


async def test_dispatch_lab_due_renders_freeform_buttons_in_csw(monkeypatch):
    captured = _patch_async_client(monkeypatch)
    _set_csw(monkeypatch, in_csw=True)
    _set_lab(monkeypatch, _lab_followup())

    err = await dispatcher_module.dispatch(
        _fake_event(
            event_type="lab_followup_due",
            payload={
                "lab_followup_id": 10,
                "patient_db_id": 2,
                "test_name": "HbA1c",
                "stage": "d1",
            },
        ),
        db=object(),
        gateway_url="http://gw:1",
    )
    assert err is None
    body = captured["json"]
    assert body["use_template"] is False
    assert "HbA1c" in body["body"]
    button_labels = [b["label"] for b in body["buttons"]]
    # status=due → all 3 buttons (Booked + Completed + Need help)
    assert button_labels == ["Booked", "Completed", "Need help"]


async def test_dispatch_lab_due_when_booked_skips_booked_button(monkeypatch):
    captured = _patch_async_client(monkeypatch)
    _set_csw(monkeypatch, in_csw=True)
    _set_lab(monkeypatch, _lab_followup(status=FollowupStatus.booked))

    err = await dispatcher_module.dispatch(
        _fake_event(
            event_type="lab_followup_due",
            payload={
                "lab_followup_id": 10,
                "test_name": "HbA1c",
                "stage": "overdue",
            },
        ),
        db=object(),
        gateway_url="http://gw:1",
    )
    assert err is None
    button_labels = [b["label"] for b in captured["json"]["buttons"]]
    # status=booked → no "Booked" button (already booked); just Completed +
    # Need help.
    assert button_labels == ["Completed", "Need help"]


async def test_dispatch_lab_due_skips_when_completed(monkeypatch):
    def must_not_call(url, json):  # noqa: ARG001
        raise AssertionError("not_applicable event must not hit gateway")

    _patch_async_client(monkeypatch, post=must_not_call)
    _set_csw(monkeypatch, in_csw=True)
    _set_lab(monkeypatch, _lab_followup(status=FollowupStatus.completed))

    err = await dispatcher_module.dispatch(
        _fake_event(
            event_type="lab_followup_due",
            payload={
                "lab_followup_id": 10,
                "test_name": "HbA1c",
                "stage": "d1",
            },
        ),
        db=object(),
        gateway_url="http://gw:1",
    )
    assert err is not None
    assert err.startswith("not_applicable:")


async def test_dispatch_lab_due_template_outside_csw(monkeypatch):
    captured = _patch_async_client(monkeypatch)
    _set_csw(monkeypatch, in_csw=False)
    _set_lab(monkeypatch, _lab_followup())

    err = await dispatcher_module.dispatch(
        _fake_event(
            event_type="lab_followup_due",
            payload={
                "lab_followup_id": 10,
                "test_name": "HbA1c",
                "stage": "d1",
            },
        ),
        db=object(),
        gateway_url="http://gw:1",
    )
    assert err is None
    body = captured["json"]
    assert body["use_template"] is True
    assert body["template_name"] == "lab_closure_update_v1"
    assert body["template_params"]["1_test"] == "HbA1c"


async def test_dispatch_dose_due_skips_when_already_taken(monkeypatch):
    def must_not_call(url, json):  # noqa: ARG001
        raise AssertionError("not_applicable event must not hit gateway")

    _patch_async_client(monkeypatch, post=must_not_call)
    _set_csw(monkeypatch, in_csw=True)
    taken_adherence = types.SimpleNamespace(
        id=42,
        status=AdherenceStatus.taken,
        scheduled_at=datetime.now(timezone.utc),
        regimen_id=1,
        patient_id=2,
    )
    _set_adherence(monkeypatch, adherence=taken_adherence)

    err = await dispatcher_module.dispatch(
        _fake_event(
            event_type="dose_due",
            payload={
                "adherence_event_id": 42,
                "regimen_id": 1,
                "medication_name": "Metformin",
                "dose": "500 mg",
                "scheduled_at_iso": datetime.now(timezone.utc).isoformat(),
            },
        ),
        db=object(),
        gateway_url="http://gw:1",
    )
    assert err is not None
    assert err.startswith("not_applicable:")

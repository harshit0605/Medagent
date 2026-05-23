"""Unit tests for the booking_agent ReAct loop.

We mock both ends:
- ``AgentLLM`` is replaced with a stub that returns scripted tool_calls then a
  final assistant message.
- The calendar primitives + repos are patched at the booking_agent module's
  import boundary so no DB / Google traffic happens.

The tests assert:
- Cancellation short-circuits without an LLM call.
- A "list doctors → find slots → book slot" three-call sequence drives the
  ReAct loop to a final reply with current_flow cleared.
- A failing tool call surfaces a friendly response and keeps current_flow="booking".
- The MAX_TOOL_STEPS cap exits cleanly.
"""

from __future__ import annotations

import json
from typing import Any



# ---- shared fakes -----------------------------------------------------------


class _FakeLLM:
    """Replaces AgentLLM: enabled=True, scripted chat_with_tools responses."""

    def __init__(self, scripted: list[dict[str, Any] | None]):
        self.scripted = list(scripted)
        self.calls: list[dict[str, Any]] = []
        self.enabled = True

    async def chat_with_tools(self, **kwargs):
        self.calls.append(kwargs)
        return self.scripted.pop(0)


class _NoopAsyncSession:
    """Stand-in for AsyncSession so booking_agent's `async with SessionLocal() as db:`
    doesn't need a real DB. Tool impls are mocked so the session is rarely
    used — but the runner now calls `doctors_repo.get(db, ...)` after a
    successful find_slots to enrich list-row descriptions, so we provide a
    no-op `get` that returns None (the runner has a fallback naming path)."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None

    async def commit(self):
        return None

    async def get(self, *_args, **_kwargs):
        return None


def _patch_session(monkeypatch):
    from services.orchestrator import booking_agent

    def factory():
        return _NoopAsyncSession()

    # `get_sessionmaker()` is called once per turn; we replace its return value.
    monkeypatch.setattr(booking_agent, "get_sessionmaker", lambda: factory)


def _patch_llm(monkeypatch, fake: _FakeLLM):
    from services.orchestrator import booking_agent

    monkeypatch.setattr(booking_agent, "get_llm", lambda: fake)


def _make_tool_call(name: str, args: dict[str, Any], call_id: str = "call_1") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


# ---- 1. Flow-abort short-circuits (bare keyword only) --------------------


async def test_bare_cancel_aborts_flow_without_llm(monkeypatch):
    from services.orchestrator.booking_agent import run_booking_agent

    fake_llm = _FakeLLM(scripted=[])  # would error if called
    _patch_llm(monkeypatch, fake_llm)
    _patch_session(monkeypatch)

    delta = await run_booking_agent(
        patient_phone="918340858764",
        patient_db_id=1,
        state_messages=[],
        new_user_text="cancel",
        flow_state={"some": "state"},
    )

    assert delta["current_flow"] is None
    assert delta["flow_state"] == {}
    assert "dropped" in delta["response_body"].lower()
    assert fake_llm.calls == []  # never invoked


def test_flow_abort_heuristic_does_not_swallow_cancel_my_appointment():
    """'cancel my appointment' is an INTENT (route to cancel_appointment tool),
    NOT a flow abort. Must reach the LLM."""
    from services.orchestrator.booking_agent import _looks_like_flow_abort

    assert _looks_like_flow_abort("cancel") is True
    assert _looks_like_flow_abort("Cancel.") is True
    assert _looks_like_flow_abort("never mind") is True
    # These MUST go through the LLM:
    assert _looks_like_flow_abort("cancel my appointment") is False
    assert _looks_like_flow_abort("cancel my booking with Dr Harshit") is False
    assert _looks_like_flow_abort("can I cancel?") is False


def test_strip_enumerated_lines_drops_numbered_and_reply_hint():
    from services.orchestrator.booking_agent import _strip_enumerated_lines

    body = (
        "Pick a slot with Dr X:\n"
        "1. Mon 04 May, 09:00 AM\n"
        "2) Mon 04 May, 09:30 AM\n"
        "Reply with the number to book."
    )
    cleaned = _strip_enumerated_lines(body)
    assert cleaned == "Pick a slot with Dr X:"


def test_detect_reschedule_target_only_checks_current_turn():
    """Detection MUST come from the current inbound text only, never from
    historical messages — the per-patient LangGraph checkpoint preserves
    prior turns indefinitely, so a stale 'reschedule appt 3' from a
    cancelled-and-completed flow would otherwise falsely tag today's fresh
    booking as a reschedule."""
    from services.orchestrator.booking_agent import _detect_reschedule_target

    # Current message has it → detected.
    assert (
        _detect_reschedule_target("Please reschedule my appointment id 7.", [])
        == 7
    )
    # Historical user message has it but current is unrelated → NOT detected.
    history = [
        {"role": "user", "content": "Please reschedule my appointment id 12."},
        {"role": "assistant", "content": "Choose a new time:"},
    ]
    assert (
        _detect_reschedule_target("book Mon morning with Dr Harshit", history)
        is None
    )
    # Plain booking — no reschedule context.
    assert _detect_reschedule_target("book me with Dr X tomorrow", []) is None


# ---- 2. Happy path: list -> find -> book ----------------------------------


async def test_happy_path_list_find_book(monkeypatch):
    from services.orchestrator import booking_agent
    from services.orchestrator.booking_agent import run_booking_agent

    # Patch tool implementations directly so we don't need DB or Google.
    async def stub_list(_db, _args):
        return json.dumps(
            [{"id": 1, "name": "Dr Harshit", "timezone": "Asia/Kolkata", "calendar_id": "primary"}]
        )

    async def stub_find(_db, _args):
        return json.dumps(
            {
                "doctor_id": 1,
                "timezone": "Asia/Kolkata",
                "duration_minutes": 30,
                "free": [
                    {
                        "start": "2026-05-02T10:00:00+05:30",
                        "end": "2026-05-02T10:30:00+05:30",
                        "local": "Sat 02 May, 10:00 AM",
                    }
                ],
            }
        )

    async def stub_book(_db, args):
        # The agent must pass the patient_db_id we put in the system prompt.
        assert args["patient_db_id"] == 7
        assert args["patient_phone"] == "918340858764"
        return json.dumps(
            {
                "ok": True,
                "appointment_id": 99,
                "calendar_event_id": "evt-abc",
                "html_link": "https://calendar/evt-abc",
                "start": args["start"],
                "end": args["end"],
            }
        )

    monkeypatch.setitem(booking_agent._TOOL_DISPATCH, "list_connected_doctors", stub_list)
    monkeypatch.setitem(booking_agent._TOOL_DISPATCH, "find_slots", stub_find)
    monkeypatch.setitem(booking_agent._TOOL_DISPATCH, "book_slot", stub_book)

    fake_llm = _FakeLLM(
        scripted=[
            # Step 1: LLM asks who the doctors are.
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_make_tool_call("list_connected_doctors", {}, "c1")],
            },
            # Step 2: LLM asks for free slots.
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    _make_tool_call(
                        "find_slots",
                        {
                            "doctor_id": 1,
                            "start": "2026-05-02T09:00:00+05:30",
                            "end": "2026-05-02T18:00:00+05:30",
                            "duration_minutes": 30,
                        },
                        "c2",
                    )
                ],
            },
            # Step 3: LLM books the only slot.
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    _make_tool_call(
                        "book_slot",
                        {
                            "doctor_id": 1,
                            "start": "2026-05-02T10:00:00+05:30",
                            "end": "2026-05-02T10:30:00+05:30",
                            "summary": "Patient consultation — 918340858764",
                            "patient_db_id": 7,
                            "patient_phone": "918340858764",
                        },
                        "c3",
                    )
                ],
            },
            # Step 4: final natural-language confirmation.
            {
                "role": "assistant",
                "content": "Booked! Sat 02 May at 10:00 AM with Dr Harshit ✓",
            },
        ]
    )
    _patch_llm(monkeypatch, fake_llm)
    _patch_session(monkeypatch)

    delta = await run_booking_agent(
        patient_phone="918340858764",
        patient_db_id=7,
        state_messages=[{"role": "user", "content": "Book Sat morning with Dr Harshit"}],
        new_user_text="Book Sat morning with Dr Harshit",
        flow_state=None,
    )

    assert delta["current_flow"] is None  # cleared on successful booking
    assert delta["flow_state"]["last_action"] == "book_slot"
    assert delta["flow_state"]["appointment_id"] == 99
    assert "Sat 02 May" in delta["response_body"]
    # Four LLM calls happened (one per scripted response).
    assert len(fake_llm.calls) == 4
    # And new_messages should include both assistant turns + the three tool messages.
    msg_roles = [m.get("role") for m in delta["messages"]]
    assert msg_roles.count("tool") == 3
    assert msg_roles.count("assistant") == 4
    # Booking confirmation must carry tappable Cancel + Reschedule buttons that
    # reference the booked appointment id.
    button_labels = [b["label"] for b in delta["buttons"]]
    assert button_labels == ["Cancel appointment", "Reschedule"]
    assert all("99" in b["id"] for b in delta["buttons"])
    # Booking already completed → no slot list_rows attached.
    assert delta["list_rows"] == []


# ---- 2.5 Slot list rendering --------------------------------------------


async def test_find_slots_without_book_attaches_list_rows(monkeypatch):
    """When find_slots returns multiple options and the LLM ends with a
    'pick a slot' reply (no immediate book_slot), the runner must attach
    a tappable WhatsApp list with one row per slot."""
    from services.orchestrator import booking_agent
    from services.orchestrator.booking_agent import run_booking_agent

    async def stub_find(_db, _args):
        return json.dumps(
            {
                "doctor_id": 1,
                "timezone": "Asia/Kolkata",
                "duration_minutes": 30,
                "free": [
                    {
                        "start": "2026-05-04T09:00:00+05:30",
                        "end": "2026-05-04T09:30:00+05:30",
                        "local": "Mon 04 May, 09:00 AM",
                    },
                    {
                        "start": "2026-05-04T09:30:00+05:30",
                        "end": "2026-05-04T10:00:00+05:30",
                        "local": "Mon 04 May, 09:30 AM",
                    },
                    {
                        "start": "2026-05-04T10:00:00+05:30",
                        "end": "2026-05-04T10:30:00+05:30",
                        "local": "Mon 04 May, 10:00 AM",
                    },
                ],
            }
        )

    monkeypatch.setitem(booking_agent._TOOL_DISPATCH, "find_slots", stub_find)

    fake_llm = _FakeLLM(
        scripted=[
            # Step 1: LLM looks up free slots.
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    _make_tool_call(
                        "find_slots",
                        {
                            "doctor_id": 1,
                            "start": "2026-05-04T09:00:00+05:30",
                            "end": "2026-05-04T12:00:00+05:30",
                            "duration_minutes": 30,
                        },
                        "c1",
                    )
                ],
            },
            # Step 2: header-only reply — runner attaches the slots as a list.
            {
                "role": "assistant",
                "content": "Pick a slot with Dr Harshit:",
            },
        ]
    )
    _patch_llm(monkeypatch, fake_llm)
    _patch_session(monkeypatch)

    delta = await run_booking_agent(
        patient_phone="918340858764",
        patient_db_id=7,
        state_messages=[{"role": "user", "content": "book Mon morning with Dr Harshit"}],
        new_user_text="book Mon morning with Dr Harshit",
        flow_state=None,
    )

    assert delta["current_flow"] == "booking"  # still mid-flow until pick
    assert delta["response_body"] == "Pick a slot with Dr Harshit:"
    rows = delta["list_rows"]
    assert len(rows) == 3
    assert rows[0]["title"] == "Mon 04 May, 09:00 AM"
    # Book context (no reschedule_target) → row id uses slot_book: prefix.
    assert rows[0]["id"].startswith("slot_book:1|")
    assert "30 min" in rows[0]["description"]
    assert delta["list_button_label"] == "Pick a slot"
    assert delta["list_section_title"] == "Available times"


async def test_find_slots_during_reschedule_uses_slot_resched_row_ids(monkeypatch):
    """When the inbound text says 'Please reschedule my appointment id N',
    the slot list rows must encode action=reschedule so a tap re-routes to
    reschedule_appointment (not book_slot)."""
    from services.orchestrator import booking_agent
    from services.orchestrator.booking_agent import run_booking_agent

    async def stub_find(_db, _args):
        return json.dumps(
            {
                "doctor_id": 1,
                "timezone": "Asia/Kolkata",
                "duration_minutes": 30,
                "free": [
                    {
                        "start": "2026-05-04T11:30:00+05:30",
                        "end": "2026-05-04T12:00:00+05:30",
                        "local": "Mon 04 May, 11:30 AM",
                    },
                ],
            }
        )

    monkeypatch.setitem(booking_agent._TOOL_DISPATCH, "find_slots", stub_find)

    fake_llm = _FakeLLM(
        scripted=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    _make_tool_call(
                        "find_slots",
                        {
                            "doctor_id": 1,
                            "start": "2026-05-04T09:00:00+05:30",
                            "end": "2026-05-04T12:00:00+05:30",
                            "duration_minutes": 30,
                        },
                        "c1",
                    )
                ],
            },
            {"role": "assistant", "content": "Choose a new time for Dr Harshit:"},
        ]
    )
    _patch_llm(monkeypatch, fake_llm)
    _patch_session(monkeypatch)

    delta = await run_booking_agent(
        patient_phone="918340858764",
        patient_db_id=7,
        state_messages=[],
        new_user_text="Please reschedule my appointment id 3.",
        flow_state=None,
    )

    rows = delta["list_rows"]
    assert len(rows) == 1
    # Reschedule context → slot_resched: prefix with appt id 3.
    assert rows[0]["id"].startswith("slot_resched:3|1|")
    assert delta["list_button_label"] == "Pick a new time"


# ---- 3. Tool failure keeps flow in progress ------------------------------


async def test_tool_error_keeps_flow_alive(monkeypatch):
    from services.orchestrator import booking_agent
    from services.orchestrator.booking_agent import run_booking_agent

    async def stub_list_fails(_db, _args):
        return "ERROR: doctor 1 oauth_status=expired; reconnect required"

    monkeypatch.setitem(
        booking_agent._TOOL_DISPATCH, "list_connected_doctors", stub_list_fails
    )

    fake_llm = _FakeLLM(
        scripted=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_make_tool_call("list_connected_doctors", {}, "c1")],
            },
            {
                "role": "assistant",
                "content": "Sorry, our doctor is offline right now. Reply CALL for help.",
            },
        ]
    )
    _patch_llm(monkeypatch, fake_llm)
    _patch_session(monkeypatch)

    delta = await run_booking_agent(
        patient_phone="918340858764",
        patient_db_id=7,
        state_messages=[],
        new_user_text="book me a slot",
        flow_state=None,
    )

    # LLM gave a final response (no booking_completed), so flow stays in "booking"
    # so the next user message routes back here.
    assert delta["current_flow"] == "booking"
    assert "offline" in delta["response_body"].lower() or "call" in delta["response_body"].lower()


# ---- 4. LLM disabled / unreachable bails cleanly -------------------------


async def test_llm_unreachable_returns_friendly_failure(monkeypatch):
    fake_llm = _FakeLLM(scripted=[None])  # chat_with_tools returns None
    _patch_llm(monkeypatch, fake_llm)
    _patch_session(monkeypatch)

    from services.orchestrator.booking_agent import run_booking_agent

    delta = await run_booking_agent(
        patient_phone="918340858764",
        patient_db_id=7,
        state_messages=[],
        new_user_text="book me",
        flow_state=None,
    )

    assert "trouble" in delta["response_body"].lower()
    # Stay in the flow so a retry routes back.
    assert delta["current_flow"] == "booking"


# ---- 2b. Cancel happy path -----------------------------------------------


async def test_cancel_appointment_happy_path(monkeypatch):
    from services.orchestrator import booking_agent
    from services.orchestrator.booking_agent import run_booking_agent

    async def stub_list(_db, args):
        assert args["patient_db_id"] == 7
        return json.dumps(
            {
                "appointments": [
                    {
                        "appointment_id": 99,
                        "doctor_id": 1,
                        "doctor_name": "Dr Harshit",
                        "scheduled_for": "2026-05-03T11:00:00+05:30",
                        "scheduled_for_local": "Sun 03 May, 11:00 AM",
                        "end_at": "2026-05-03T11:30:00+05:30",
                        "status": "confirmed",
                        "calendar_html_link": "https://calendar/evt-xyz",
                    }
                ]
            }
        )

    async def stub_cancel(_db, args):
        assert args["appointment_id"] == 99
        assert args["patient_db_id"] == 7  # ownership param now required
        return json.dumps({"ok": True, "appointment_id": 99, "status": "cancelled"})

    monkeypatch.setitem(booking_agent._TOOL_DISPATCH, "list_my_appointments", stub_list)
    monkeypatch.setitem(booking_agent._TOOL_DISPATCH, "cancel_appointment", stub_cancel)

    fake_llm = _FakeLLM(
        scripted=[
            # Step 1: agent looks up the patient's appointments.
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    _make_tool_call("list_my_appointments", {"patient_db_id": 7}, "c1")
                ],
            },
            # Step 2: with one appointment present, agent cancels it directly.
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    _make_tool_call(
                        "cancel_appointment",
                        {"appointment_id": 99, "patient_db_id": 7},
                        "c2",
                    )
                ],
            },
            # Step 3: confirmation reply.
            {"role": "assistant", "content": "Cancelled your Sun 03 May, 11:00 AM with Dr Harshit."},
        ]
    )
    _patch_llm(monkeypatch, fake_llm)
    _patch_session(monkeypatch)

    delta = await run_booking_agent(
        patient_phone="918340858764",
        patient_db_id=7,
        state_messages=[{"role": "user", "content": "cancel my appointment"}],
        new_user_text="cancel my appointment",
        flow_state=None,
    )

    assert delta["current_flow"] is None  # cleared on successful cancel
    assert delta["flow_state"]["last_action"] == "cancel_appointment"
    assert delta["flow_state"]["appointment_id"] == 99
    assert "cancelled" in delta["response_body"].lower()
    # Cancellation confirmation gets a single "Book another" button.
    assert [b["label"] for b in delta["buttons"]] == ["Book another"]


# ---- 2c. Reschedule happy path -------------------------------------------


async def test_reschedule_appointment_happy_path(monkeypatch):
    from services.orchestrator import booking_agent
    from services.orchestrator.booking_agent import run_booking_agent

    async def stub_list(_db, _args):
        return json.dumps(
            {
                "appointments": [
                    {
                        "appointment_id": 99,
                        "doctor_id": 1,
                        "doctor_name": "Dr Harshit",
                        "scheduled_for": "2026-05-03T11:00:00+05:30",
                        "scheduled_for_local": "Sun 03 May, 11:00 AM",
                        "end_at": "2026-05-03T11:30:00+05:30",
                        "status": "confirmed",
                        "calendar_html_link": "https://calendar/evt-xyz",
                    }
                ]
            }
        )

    async def stub_find(_db, _args):
        return json.dumps(
            {
                "doctor_id": 1,
                "timezone": "Asia/Kolkata",
                "duration_minutes": 30,
                "free": [
                    {
                        "start": "2026-05-04T09:00:00+05:30",
                        "end": "2026-05-04T09:30:00+05:30",
                        "local": "Mon 04 May, 09:00 AM",
                    }
                ],
            }
        )

    async def stub_reschedule(_db, args):
        assert args["appointment_id"] == 99
        assert args["patient_db_id"] == 7  # ownership param now required
        return json.dumps(
            {
                "ok": True,
                "appointment_id": 99,
                "new_start": args["new_start"],
                "new_end": args["new_end"],
                "html_link": "https://calendar/evt-xyz",
            }
        )

    monkeypatch.setitem(booking_agent._TOOL_DISPATCH, "list_my_appointments", stub_list)
    monkeypatch.setitem(booking_agent._TOOL_DISPATCH, "find_slots", stub_find)
    monkeypatch.setitem(
        booking_agent._TOOL_DISPATCH, "reschedule_appointment", stub_reschedule
    )

    fake_llm = _FakeLLM(
        scripted=[
            # 1. List patient's appointments to find what to move.
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    _make_tool_call("list_my_appointments", {"patient_db_id": 7}, "c1")
                ],
            },
            # 2. Look up free slots on Monday morning for the same doctor.
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    _make_tool_call(
                        "find_slots",
                        {
                            "doctor_id": 1,
                            "start": "2026-05-04T09:00:00+05:30",
                            "end": "2026-05-04T12:00:00+05:30",
                            "duration_minutes": 30,
                        },
                        "c2",
                    )
                ],
            },
            # 3. Patient sees one option; agent reschedules to it.
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    _make_tool_call(
                        "reschedule_appointment",
                        {
                            "appointment_id": 99,
                            "patient_db_id": 7,
                            "new_start": "2026-05-04T09:00:00+05:30",
                            "new_end": "2026-05-04T09:30:00+05:30",
                        },
                        "c3",
                    )
                ],
            },
            # 4. Final confirmation.
            {"role": "assistant", "content": "Moved to Mon 04 May, 09:00 AM."},
        ]
    )
    _patch_llm(monkeypatch, fake_llm)
    _patch_session(monkeypatch)

    delta = await run_booking_agent(
        patient_phone="918340858764",
        patient_db_id=7,
        state_messages=[{"role": "user", "content": "move my appointment to monday morning"}],
        new_user_text="move my appointment to monday morning",
        flow_state=None,
    )

    assert delta["current_flow"] is None
    assert delta["flow_state"]["last_action"] == "reschedule_appointment"
    assert delta["flow_state"]["appointment_id"] == 99
    assert "mon" in delta["response_body"].lower() or "moved" in delta["response_body"].lower()
    # Reschedule confirmation gets Cancel + Reschedule-again buttons.
    button_labels = [b["label"] for b in delta["buttons"]]
    assert button_labels == ["Cancel appointment", "Reschedule again"]
    assert all("99" in b["id"] for b in delta["buttons"])


# ---- sanitizer ------------------------------------------------------------


def test_sanitize_history_drops_orphan_tool_messages():
    from services.orchestrator.booking_agent import _sanitize_history

    history = [
        {"role": "user", "content": "hi"},
        # orphan tool message — never preceded by a matching assistant.tool_calls
        {"role": "tool", "tool_call_id": "ghost", "content": "stale"},
        {"role": "assistant", "content": "How can I help?"},
    ]
    cleaned = _sanitize_history(history)
    assert [m["role"] for m in cleaned] == ["user", "assistant"]


def test_sanitize_history_drops_orphan_tool_calls_assistant():
    from services.orchestrator.booking_agent import _sanitize_history

    history = [
        {"role": "user", "content": "book"},
        # assistant requested a tool but no tool result followed
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "x", "arguments": "{}"}}],
        },
        {"role": "user", "content": "still here"},
    ]
    cleaned = _sanitize_history(history)
    assert [m["role"] for m in cleaned] == ["user", "user"]


def test_sanitize_history_keeps_well_formed_tool_round_trip():
    from services.orchestrator.booking_agent import _sanitize_history

    history = [
        {"role": "user", "content": "book"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "x", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        {"role": "assistant", "content": "Done."},
    ]
    cleaned = _sanitize_history(history)
    assert [m["role"] for m in cleaned] == ["user", "assistant", "tool", "assistant"]


# ---- ownership enforcement on cancel + reschedule ------------------------


class _FakeAppt:
    def __init__(self, *, id: int, patient_id: int, status_value: str = "confirmed"):
        from app.db.models import AppointmentStatus

        self.id = id
        self.patient_id = patient_id
        self.doctor_id = 1
        self.status = AppointmentStatus(status_value)
        self.calendar_event_id = "evt-fake"


async def test_cancel_appointment_refuses_cross_patient(monkeypatch):
    """If the LLM hallucinates an id belonging to another patient, the tool
    must return ERROR — not silently cancel someone else's appointment."""
    from services.orchestrator.booking_agent import _tool_cancel_appointment
    from app.db.repositories import appointments as appt_repo

    appt = _FakeAppt(id=99, patient_id=42)  # belongs to patient 42

    async def fake_get(_db, aid):
        return appt if aid == 99 else None

    monkeypatch.setattr(appt_repo, "get", fake_get)

    result = await _tool_cancel_appointment(
        None, {"appointment_id": 99, "patient_db_id": 7}  # caller is patient 7
    )
    assert result.startswith("ERROR")
    assert "does not belong" in result


async def test_reschedule_appointment_refuses_cross_patient(monkeypatch):
    from services.orchestrator.booking_agent import _tool_reschedule_appointment
    from app.db.repositories import appointments as appt_repo

    appt = _FakeAppt(id=99, patient_id=42)

    async def fake_get(_db, aid):
        return appt if aid == 99 else None

    monkeypatch.setattr(appt_repo, "get", fake_get)

    result = await _tool_reschedule_appointment(
        None,
        {
            "appointment_id": 99,
            "patient_db_id": 7,
            "new_start": "2026-05-04T09:00:00+05:30",
            "new_end": "2026-05-04T09:30:00+05:30",
        },
    )
    assert result.startswith("ERROR")
    assert "does not belong" in result


# ---- 5. Max-step safety cap -----------------------------------------------


async def test_max_steps_cap_aborts_cleanly(monkeypatch):
    from services.orchestrator import booking_agent
    from services.orchestrator.booking_agent import MAX_TOOL_STEPS, run_booking_agent

    # Loop forever: every LLM response asks for another tool call.
    async def stub_list(_db, _args):
        return "[]"

    monkeypatch.setitem(booking_agent._TOOL_DISPATCH, "list_connected_doctors", stub_list)

    fake_llm = _FakeLLM(
        scripted=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_make_tool_call("list_connected_doctors", {}, f"c{i}")],
            }
            for i in range(MAX_TOOL_STEPS + 2)
        ]
    )
    _patch_llm(monkeypatch, fake_llm)
    _patch_session(monkeypatch)

    delta = await run_booking_agent(
        patient_phone="918340858764",
        patient_db_id=7,
        state_messages=[],
        new_user_text="book",
        flow_state=None,
    )

    assert delta["current_flow"] is None  # cap exits the flow
    assert "trouble" in delta["response_body"].lower() or "start over" in delta["response_body"].lower()
    # Used exactly MAX_TOOL_STEPS LLM calls.
    assert len(fake_llm.calls) == MAX_TOOL_STEPS

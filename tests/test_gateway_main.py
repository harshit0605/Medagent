"""Smoke checks that the WhatsApp gateway module imports cleanly.

The previous in-memory ring buffer has been replaced with a Postgres-backed
message_log table; bounded-ring-buffer behavior is no longer relevant. The
end-to-end webhook/send/logs round-trip is exercised by
tests/integration/test_persistence.py against a real database.
"""

from __future__ import annotations

from services.whatsapp_gateway import main as gateway_main
from services.whatsapp_gateway import meta


def test_gateway_app_routes_registered():
    paths = {route.path for route in gateway_main.app.routes}
    assert {"/health", "/webhook", "/send", "/logs"}.issubset(paths)


def test_interactive_buttons_body_shape_matches_meta_spec():
    """Sanity check the JSON shape we send to Meta for interactive buttons."""
    body = meta._build_interactive_buttons_body(
        to="+14155550100",
        text="Reminder body",
        buttons=[
            {"id": "cancel_appt:42", "title": "Cancel appointment"},
            {"id": "reschedule_appt:42", "title": "Reschedule"},
        ],
    )
    assert body["type"] == "interactive"
    assert body["interactive"]["type"] == "button"
    assert body["interactive"]["body"]["text"] == "Reminder body"
    btns = body["interactive"]["action"]["buttons"]
    assert len(btns) == 2
    assert btns[0] == {
        "type": "reply",
        "reply": {"id": "cancel_appt:42", "title": "Cancel appointment"},
    }
    # Phone normalised by stripping the leading '+'.
    assert body["to"] == "14155550100"


def test_interactive_buttons_caps_at_three_and_truncates_titles():
    body = meta._build_interactive_buttons_body(
        to="14155550100",
        text="Pick one",
        buttons=[
            {"id": "a", "title": "A" * 50},  # title too long → trimmed to 20
            {"id": "b", "title": "B"},
            {"id": "c", "title": "C"},
            {"id": "d", "title": "D"},  # 4th must be dropped (Meta limit = 3)
        ],
    )
    btns = body["interactive"]["action"]["buttons"]
    assert len(btns) == 3
    assert len(btns[0]["reply"]["title"]) == 20


def test_interactive_list_body_shape_matches_meta_spec():
    body = meta._build_interactive_list_body(
        to="14155550100",
        text="Pick a slot",
        rows=[
            {
                "id": "slot_book:1|2026-05-04T09:00:00+05:30|2026-05-04T09:30:00+05:30",
                "title": "Mon 04 May, 09:00 AM",
                "description": "30 min with Dr Harshit",
            },
            {
                "id": "slot_book:1|2026-05-04T09:30:00+05:30|2026-05-04T10:00:00+05:30",
                "title": "Mon 04 May, 09:30 AM",
                "description": "30 min with Dr Harshit",
            },
        ],
        button_label="Pick a slot",
        section_title="Available times",
    )
    assert body["type"] == "interactive"
    assert body["interactive"]["type"] == "list"
    assert body["interactive"]["body"]["text"] == "Pick a slot"
    action = body["interactive"]["action"]
    assert action["button"] == "Pick a slot"
    assert len(action["sections"]) == 1
    rows = action["sections"][0]["rows"]
    assert len(rows) == 2
    assert rows[0]["title"] == "Mon 04 May, 09:00 AM"
    assert rows[0]["description"] == "30 min with Dr Harshit"
    assert rows[0]["id"].startswith("slot_book:1|")


def test_template_body_includes_dynamic_button_payloads():
    """The outside-CSW reminder path ships a template send with per-call
    quick_reply payloads (the appointment id encoded in `cancel_appt:N` /
    `reschedule_appt:N`). Confirm the wire shape matches Meta's spec."""
    body = meta._build_template_body(
        to="14155550100",
        template_name="appointment_reminder_v1",
        template_params={"reminder_text": "Reminder body here."},
        button_payloads=[
            {"sub_type": "quick_reply", "index": "0", "payload": "cancel_appt:7"},
            {
                "sub_type": "quick_reply",
                "index": "1",
                "payload": "reschedule_appt:7",
            },
        ],
    )
    components = body["template"]["components"]
    # Body component first, then 2 button components.
    body_comp = next(c for c in components if c["type"] == "body")
    assert body_comp["parameters"][0]["text"] == "Reminder body here."

    btn_comps = [c for c in components if c["type"] == "button"]
    assert len(btn_comps) == 2
    assert btn_comps[0]["sub_type"] == "quick_reply"
    assert btn_comps[0]["index"] == "0"
    assert btn_comps[0]["parameters"][0] == {
        "type": "payload",
        "payload": "cancel_appt:7",
    }
    assert btn_comps[1]["index"] == "1"
    assert btn_comps[1]["parameters"][0]["payload"] == "reschedule_appt:7"


def test_interactive_list_caps_at_ten_rows_and_truncates_fields():
    rows_in = [
        {
            "id": f"slot:{i}",
            "title": "T" * 40,  # over 24-char title cap
            "description": "D" * 100,  # over 72-char description cap
        }
        for i in range(15)
    ]
    body = meta._build_interactive_list_body(
        to="14155550100",
        text="Pick",
        rows=rows_in,
        button_label="X" * 50,  # over 20-char cap
        section_title="S" * 50,  # over 24-char cap
    )
    rows_out = body["interactive"]["action"]["sections"][0]["rows"]
    assert len(rows_out) == 10
    assert len(rows_out[0]["title"]) == 24
    assert len(rows_out[0]["description"]) == 72
    assert len(body["interactive"]["action"]["button"]) == 20
    assert len(body["interactive"]["action"]["sections"][0]["title"]) == 24

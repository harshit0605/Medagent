from shared.contracts.models import Event, EventType, MessageIn, MessageOut


def test_message_in_defaults():
    m = MessageIn(message_id="m1", patient_id="p1", phone="+91123", text="Taken")
    assert m.channel == "whatsapp"


def test_message_out_template_mode():
    o = MessageOut(patient_id="p1", phone="+91123", body="Hi", use_template=True, template_name="dose_reminder_v1")
    assert o.use_template is True
    assert o.template_name == "dose_reminder_v1"


def test_event_types():
    e = Event(event_type=EventType.DOSE_DUE, patient_id="p1")
    assert e.event_type.value == "dose_due"

import json
from pathlib import Path

from pydantic import TypeAdapter

from shared.contracts.models import Event, MessageIn, MessageOut


EXAMPLES_DIR = Path("shared/contracts/examples")
EVENT_ADAPTER = TypeAdapter(Event)


def _read_example(name: str) -> dict:
    return json.loads((EXAMPLES_DIR / name).read_text())


def test_message_in_example_validates() -> None:
    data = _read_example("message_in.json")
    parsed = MessageIn.model_validate(data)
    assert parsed.message_id == "msg_in_001"


def test_message_out_example_validates() -> None:
    data = _read_example("message_out.json")
    parsed = MessageOut.model_validate(data)
    assert parsed.correlation_id == "corr_12345"


def test_event_examples_validate() -> None:
    event_files = [
        "event_dose_due.json",
        "event_dose_confirmed.json",
        "event_dose_missed.json",
        "event_refill_due.json",
        "event_triage_alert.json",
    ]
    for event_file in event_files:
        parsed = EVENT_ADAPTER.validate_python(_read_example(event_file))
        assert parsed.event_id.startswith("evt_")

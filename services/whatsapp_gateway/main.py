from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI

from shared.contracts.models import MessageIn, MessageOut

app = FastAPI(title="whatsapp_gateway")
MESSAGE_LOG: list[dict[str, Any]] = []
MAX_LOG_ENTRIES = 1000


def _append_log(entry: dict[str, Any]) -> None:
    MESSAGE_LOG.append(entry)
    if len(MESSAGE_LOG) > MAX_LOG_ENTRIES:
        del MESSAGE_LOG[0 : len(MESSAGE_LOG) - MAX_LOG_ENTRIES]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook")
def inbound_webhook(message: MessageIn) -> dict[str, Any]:
    _append_log(
        {
            "direction": "inbound",
            "received_at": datetime.now(timezone.utc).isoformat(),
            "message": message.model_dump(mode="json"),
        }
    )
    return {"accepted": True, "message_id": message.message_id}


@app.post("/send")
def send_message(message: MessageOut) -> dict[str, Any]:
    payload_type = "template" if message.use_template else "freeform"
    _append_log(
        {
            "direction": "outbound",
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "payload_type": payload_type,
            "message": message.model_dump(mode="json"),
        }
    )
    return {"status": "queued", "payload_type": payload_type}


@app.get("/logs")
def logs() -> list[dict[str, Any]]:
    return MESSAGE_LOG

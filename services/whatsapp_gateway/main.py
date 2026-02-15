from datetime import datetime
from typing import Any

from fastapi import FastAPI

from shared.contracts.models import MessageIn, MessageOut

app = FastAPI(title="whatsapp_gateway")
MESSAGE_LOG: list[dict[str, Any]] = []


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook")
def inbound_webhook(message: MessageIn) -> dict[str, Any]:
    MESSAGE_LOG.append({"direction": "inbound", "message": message.model_dump(mode="json")})
    return {"accepted": True, "message_id": message.message_id}


@app.post("/send")
def send_message(message: MessageOut) -> dict[str, Any]:
    payload_type = "template" if message.use_template else "freeform"
    MESSAGE_LOG.append(
        {
            "direction": "outbound",
            "sent_at": datetime.utcnow().isoformat(),
            "payload_type": payload_type,
            "message": message.model_dump(mode="json"),
        }
    )
    return {"status": "queued", "payload_type": payload_type}


@app.get("/logs")
def logs() -> list[dict[str, Any]]:
    return MESSAGE_LOG

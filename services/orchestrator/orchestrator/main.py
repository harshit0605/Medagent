from fastapi import FastAPI
from pydantic import BaseModel


app = FastAPI(title="orchestrator")


class InboundEvent(BaseModel):
    source: str = "whatsapp_gateway"
    user_id: str | None = None
    message: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "orchestrator"}


@app.post("/events/inbound")
def ingest_event(event: InboundEvent) -> dict[str, str]:
    # Placeholder for future LangGraph runtime invocation.
    return {
        "status": "accepted",
        "service": "orchestrator",
        "echo": event.message,
    }

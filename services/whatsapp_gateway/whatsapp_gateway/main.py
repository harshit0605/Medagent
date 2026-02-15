import os

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


app = FastAPI(title="whatsapp_gateway")


class WebhookPayload(BaseModel):
    user_id: str
    message: str


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "whatsapp_gateway"}


@app.post("/webhook")
async def inbound_webhook(payload: WebhookPayload) -> dict[str, str]:
    orchestrator_url = os.getenv(
        "ORCHESTRATOR_URL", "http://orchestrator:8001/events/inbound"
    )

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.post(
                orchestrator_url,
                json={
                    "source": "whatsapp_gateway",
                    "user_id": payload.user_id,
                    "message": payload.message,
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"Orchestrator unreachable: {exc}")

    return {"status": "forwarded", "service": "whatsapp_gateway"}

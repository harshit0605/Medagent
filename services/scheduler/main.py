from datetime import datetime, timezone

from fastapi import FastAPI

from shared.contracts.models import Event, EventType

app = FastAPI(title="scheduler")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/emit-dose-due")
def emit_dose_due(patient_id: str, regimen_id: str) -> Event:
    return Event(
        event_type=EventType.DOSE_DUE,
        patient_id=patient_id,
        at=datetime.now(timezone.utc),
        payload={"regimen_id": regimen_id},
    )

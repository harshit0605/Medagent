import os

from fastapi import FastAPI


app = FastAPI(title="scheduler")


@app.get("/health")
def health() -> dict[str, str | bool]:
    return {
        "status": "ok",
        "service": "scheduler",
        "redis_configured": bool(os.getenv("REDIS_URL")),
    }


@app.post("/jobs/tick")
def tick() -> dict[str, str]:
    # Placeholder for APScheduler/Celery job execution hooks.
    return {"status": "queued", "service": "scheduler"}

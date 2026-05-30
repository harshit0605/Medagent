"""WhatsApp gateway — outbound dispatch only.

Inbound from Meta is handled by the Next.js ops_console route at
``apps/ops_console/src/app/api/whatsapp/webhook/route.ts`` using the
``@chat-adapter/whatsapp`` chat-sdk adapter. That handler verifies the Meta
``X-Hub-Signature-256`` header, parses the payload, and forwards each parsed
message to the orchestrator's ``/route``. The orchestrator's response is then
forwarded to this gateway's ``/send`` to actually deliver via Meta.

We keep ``/webhook`` here as an internal-only entry point (no signature
required) so internal smoke tests / curl can simulate inbound without going
through Meta. Set ``WHATSAPP_INTERNAL_WEBHOOK_ENABLED=0`` to disable it.
"""

from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from datetime import datetime as _dt

from pydantic import BaseModel, Field

from app.db.repositories import message_log as message_log_repo
from app.db.repositories import whatsapp_statuses as whatsapp_statuses_repo
from app.db.session import get_session
from services.whatsapp_gateway.meta import (
    send_freeform,
    send_interactive_buttons,
    send_interactive_list,
    send_template,
)
from shared.contracts.models import MessageIn, MessageOut

log = logging.getLogger(__name__)

# Shared-secret API key. When ``GATEWAY_API_KEY`` is set, every request except
# the health probe must present a matching ``X-API-Key`` (or
# ``Authorization: Bearer``) header — the orchestrator / scheduler attach it
# via app.gateway_auth. When UNSET the gateway refuses to boot unless
# ``ALLOW_UNAUTHENTICATED=1`` is set (dev / tests opt in explicitly), so an
# unauthenticated message-sending surface is never the silent default.
_GATEWAY_API_KEY = os.getenv("GATEWAY_API_KEY", "")
_ALLOW_UNAUTHENTICATED = os.getenv("ALLOW_UNAUTHENTICATED", "") == "1"
_AUTH_EXEMPT_EXACT: frozenset[str] = frozenset(
    {"/health", "/health/live", "/health/ready"}
)


def _check_auth_config() -> None:
    """Startup fail-closed guard. ``/send`` can deliver WhatsApp messages as
    us; running it open is only acceptable in local dev / tests, which must opt
    in via ``ALLOW_UNAUTHENTICATED=1``."""
    if not _GATEWAY_API_KEY and not _ALLOW_UNAUTHENTICATED:
        raise RuntimeError(
            "GATEWAY_API_KEY is unset and ALLOW_UNAUTHENTICATED != '1'. "
            "Refusing to start an unauthenticated WhatsApp gateway (its /send "
            "delivers messages as us). Set GATEWAY_API_KEY in production, or "
            "ALLOW_UNAUTHENTICATED=1 for local dev / tests."
        )
    if not _GATEWAY_API_KEY:
        log.warning(
            "GATEWAY_API_KEY is unset — the WhatsApp gateway is "
            "UNAUTHENTICATED (ALLOW_UNAUTHENTICATED=1). Never use this in "
            "production."
        )


def _extract_api_key(request: Request) -> str | None:
    key = request.headers.get("x-api-key")
    if key:
        return key
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    _check_auth_config()
    yield


app = FastAPI(title="whatsapp_gateway", lifespan=lifespan)


@app.middleware("http")
async def _api_key_auth(request: Request, call_next):
    if _GATEWAY_API_KEY and request.url.path not in _AUTH_EXEMPT_EXACT:
        provided = _extract_api_key(request)
        if not (provided and hmac.compare_digest(provided, _GATEWAY_API_KEY)):
            return JSONResponse(
                status_code=401,
                content={"detail": "invalid or missing API key"},
            )
    return await call_next(request)

DEFAULT_LOG_LIMIT = 1000


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness — process up?"""
    return {"status": "ok"}


@app.get("/health/live")
async def health_live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
async def health_ready(response: Response) -> dict[str, object]:
    """Readiness — DB reachable? The gateway doesn't use the LLM, so it skips
    that check; Fernet isn't used here either. 503 when the DB is down."""
    from app.health import readiness_report

    ready, report = await readiness_report(check_llm=False, check_fernet=False)
    if not ready:
        response.status_code = 503
    return report


@app.post("/webhook")
async def inbound_webhook(
    message: MessageIn, db: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """Internal-only inbound endpoint (no Meta signature required).

    Production ingress is in Next.js (chat-sdk adapter). Disable this route
    by setting ``WHATSAPP_INTERNAL_WEBHOOK_ENABLED=0``.
    """
    if os.getenv("WHATSAPP_INTERNAL_WEBHOOK_ENABLED", "1") == "0":
        raise HTTPException(status_code=404, detail="internal webhook disabled")
    await message_log_repo.append_inbound(
        db,
        patient_id=message.patient_id,
        payload=message.model_dump(mode="json"),
        occurred_at=datetime.now(timezone.utc),
    )
    return {"accepted": True, "message_id": message.message_id}


def _phone_for(message: MessageOut) -> str | None:
    """The phone we ship to Meta. Falls back to patient_id when phone is unset
    (handy for tests). Returns None when neither is available."""
    return message.phone or message.patient_id


@app.post("/send")
async def send_message(
    message: MessageOut, db: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    """Dispatch outbound to Meta.

    - ``use_template=True`` requires ``template_name`` (Pydantic enforces this)
      and routes through the template send path.
    - ``use_template=False`` sends a freeform text body (only valid inside the
      24h customer-service window).
    """
    payload_kind = "template" if message.use_template else "freeform"
    to = _phone_for(message)
    if not to:
        raise HTTPException(status_code=400, detail="no recipient: patient_id and phone are both empty")

    if message.use_template:
        if not message.template_name:
            raise HTTPException(status_code=400, detail="use_template=True requires template_name")
        # When the orchestrator/dispatcher attaches buttons alongside a
        # template send, treat each button.id as the dynamic payload for
        # the template's quick_reply button at the same index. The TITLE
        # comes from the template definition (registered in Meta Business
        # Manager) and is ignored here — only the id matters as the echoed
        # callback payload when the patient taps.
        button_payloads = (
            [
                {
                    "sub_type": "quick_reply",
                    "index": str(idx),
                    "payload": b.id,
                }
                for idx, b in enumerate(message.buttons)
            ]
            if message.buttons
            else None
        )
        result = await send_template(
            to=to,
            template_name=message.template_name,
            template_params=message.template_params or None,
            button_payloads=button_payloads,
        )
    else:
        body = message.body or message.content or ""
        if not body:
            raise HTTPException(status_code=400, detail="freeform send requires body or content")
        # Freeform-mode rendering. Three flavours, in priority order:
        #   1. list_rows present → interactive list message (≤10 rows)
        #   2. buttons present  → interactive button message (≤3 reply btns)
        #   3. plain text
        # All three log as payload_kind="freeform" — the JSON column captures
        # the actual rendering choice via the serialized MessageOut.
        if message.list_rows:
            result = await send_interactive_list(
                to=to,
                text=body,
                rows=[
                    {
                        "id": r.id,
                        "title": r.title,
                        "description": r.description or "",
                    }
                    for r in message.list_rows
                ],
                button_label=message.list_button_label or "Select",
                section_title=message.list_section_title or "Options",
            )
        elif message.buttons:
            result = await send_interactive_buttons(
                to=to,
                text=body,
                buttons=[{"id": b.id, "title": b.label} for b in message.buttons],
            )
        else:
            result = await send_freeform(to=to, text=body)

    if not result.accepted:
        # Persist the failed attempt for ops visibility before surfacing the error.
        # No wamid yet — Meta didn't accept the send. The row still
        # counts as an outbound attempt in delivery metrics, where it
        # rolls up under "failed_pre_meta" (no wamid → no status row
        # to join).
        await message_log_repo.append_outbound(
            db,
            patient_id=message.patient_id,
            payload_kind=payload_kind,
            payload={
                **message.model_dump(mode="json"),
                "_send_error": result.error,
                "_dry_run": result.dry_run,
            },
            occurred_at=datetime.now(timezone.utc),
            wamid=None,
        )
        raise HTTPException(status_code=502, detail=f"meta send failed: {result.error}")

    await message_log_repo.append_outbound(
        db,
        patient_id=message.patient_id,
        payload_kind=payload_kind,
        payload={
            **message.model_dump(mode="json"),
            "_wamid": result.wamid,
            "_dry_run": result.dry_run,
        },
        occurred_at=datetime.now(timezone.utc),
        wamid=result.wamid,
    )
    return {
        "status": "queued",
        "payload_type": payload_kind,
        "dry_run": result.dry_run,
        "wamid": result.wamid,
    }


@app.get("/logs")
async def logs(
    db: AsyncSession = Depends(get_session),
    limit: int = Query(default=DEFAULT_LOG_LIMIT, ge=1, le=5000),
) -> list[dict[str, Any]]:
    rows = await message_log_repo.recent(db, limit=limit)
    return [message_log_repo.to_log_dict(row) for row in rows]


# ---- WhatsApp delivery / read receipt endpoints --------------------------------


class WhatsAppStatusRequest(BaseModel):
    """Single status update normalised from a Meta webhook payload.

    Posted by the Next.js webhook handler at /api/whatsapp/webhook after it
    parses out the `entry[].changes[].value.statuses[]` array.
    """

    wamid: str = Field(min_length=1)
    status: str = Field(pattern="^(sent|delivered|read|failed)$")
    recipient_id: str | None = None
    timestamp: _dt
    error_code: int | None = None
    error_title: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class WhatsAppStatusDTO(BaseModel):
    wamid: str
    status: str
    recipient_id: str | None = None
    timestamp: _dt
    error_code: int | None = None
    error_title: str | None = None
    updated_at: _dt


@app.post("/internal/whatsapp/status", response_model=WhatsAppStatusDTO)
async def receive_whatsapp_status(
    payload: WhatsAppStatusRequest, db: AsyncSession = Depends(get_session)
) -> WhatsAppStatusDTO:
    row = await whatsapp_statuses_repo.upsert(
        db,
        wamid=payload.wamid,
        status=payload.status,
        recipient_id=payload.recipient_id,
        timestamp=payload.timestamp,
        error_code=payload.error_code,
        error_title=payload.error_title,
        raw=payload.raw,
    )
    return WhatsAppStatusDTO(
        wamid=row.wamid,
        status=row.status,
        recipient_id=row.recipient_id,
        timestamp=row.timestamp,
        error_code=row.error_code,
        error_title=row.error_title,
        updated_at=row.updated_at,
    )


@app.get("/whatsapp/statuses", response_model=list[WhatsAppStatusDTO])
async def list_whatsapp_statuses(
    db: AsyncSession = Depends(get_session),
    recipient_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[WhatsAppStatusDTO]:
    rows = await whatsapp_statuses_repo.recent(
        db, recipient_id=recipient_id, limit=limit
    )
    return [
        WhatsAppStatusDTO(
            wamid=row.wamid,
            status=row.status,
            recipient_id=row.recipient_id,
            timestamp=row.timestamp,
            error_code=row.error_code,
            error_title=row.error_title,
            updated_at=row.updated_at,
        )
        for row in rows
    ]

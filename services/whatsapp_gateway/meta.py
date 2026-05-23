"""Async client for the WhatsApp Business Cloud API (outbound only).

Sends both freeform (in-window) and template (out-of-window) messages.
Inbound webhook receipt + signature verification + payload parsing live in
the Next.js ops_console (apps/ops_console/src/app/api/whatsapp/webhook/) using
the chat-sdk WhatsApp adapter — that piece is intentionally NOT here.

Meta Cloud API reference:
- POST https://graph.facebook.com/{version}/{phone_number_id}/messages
- Auth: Bearer {access_token}
- Freeform: type=text body
- Template: type=template with components/parameters
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MetaSendResult:
    accepted: bool
    payload_kind: str  # "template" | "freeform" | "interactive_buttons"
    dry_run: bool
    wamid: str | None
    response: dict[str, Any] | None
    error: str | None = None


# WhatsApp Cloud API hard limits for interactive button messages.
# https://developers.facebook.com/docs/whatsapp/cloud-api/messages/interactive-reply-buttons-messages
_MAX_BUTTONS = 3
_MAX_BUTTON_TITLE_CHARS = 20
_MAX_BUTTON_ID_CHARS = 256

# Interactive list message limits.
# https://developers.facebook.com/docs/whatsapp/cloud-api/messages/interactive-list-messages
_MAX_LIST_ROWS = 10
_MAX_LIST_ROW_TITLE_CHARS = 24
_MAX_LIST_ROW_DESC_CHARS = 72
_MAX_LIST_ROW_ID_CHARS = 200
_MAX_LIST_BUTTON_CHARS = 20
_MAX_LIST_SECTION_TITLE_CHARS = 24


def _is_dry_run() -> bool:
    """Dry-run if explicitly set OR if no access token is configured.

    Either condition disables real Meta calls so local dev + tests + missing
    config all behave the same — write to message_log, return a synthetic
    accepted result, no network I/O.
    """
    explicit = os.getenv("WHATSAPP_DRY_RUN")
    if explicit is not None:
        return explicit != "0"
    return not os.getenv("WHATSAPP_ACCESS_TOKEN")


def _graph_url() -> str:
    version = os.getenv("WHATSAPP_GRAPH_VERSION", "v22.0")
    phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
    return f"https://graph.facebook.com/{version}/{phone_id}/messages"


def _normalize_to(phone: str) -> str:
    """WhatsApp Cloud API accepts E.164 without the leading '+'."""
    return phone.strip().lstrip("+")


def _build_freeform_body(*, to: str, text: str) -> dict[str, Any]:
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": _normalize_to(to),
        "type": "text",
        "text": {"body": text},
    }


def _build_template_body(
    *,
    to: str,
    template_name: str,
    template_params: dict[str, str | int | float | bool] | None = None,
    language: str | None = None,
    button_payloads: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Construct the WhatsApp Cloud API template payload.

    Body params are ordered by sorted key — Meta needs positional ordering
    and our caller supplies a dict, so we flatten deterministically.

    ``button_payloads`` carries per-send dynamic data for template buttons.
    The template definition (registered in Meta Business Manager) fixes the
    button label + sub_type; this lets us inject the dynamic ``payload``
    (i.e. the id we want echoed back when the patient taps). Each entry:
      {"sub_type": "quick_reply", "index": "0", "payload": "cancel_appt:3"}
    ``index`` corresponds to the button position in the template definition.
    """
    lang = language or os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "en")
    components: list[dict[str, Any]] = []
    if template_params:
        body_params = [
            {"type": "text", "text": str(template_params[key])}
            for key in sorted(template_params)
        ]
        components.append({"type": "body", "parameters": body_params})

    if button_payloads:
        for entry in button_payloads:
            sub_type = str(entry.get("sub_type", "quick_reply"))
            idx = str(entry.get("index", "0"))
            payload_value = str(entry.get("payload", ""))
            components.append(
                {
                    "type": "button",
                    "sub_type": sub_type,
                    "index": idx,
                    "parameters": [
                        {"type": "payload", "payload": payload_value}
                    ],
                }
            )

    payload: dict[str, Any] = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": _normalize_to(to),
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": lang},
        },
    }
    if components:
        payload["template"]["components"] = components
    return payload


async def _post_to_meta(body: dict[str, Any], *, timeout: float = 10.0) -> dict[str, Any]:
    token = os.getenv("WHATSAPP_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("WHATSAPP_ACCESS_TOKEN unset; cannot call Meta")
    url = _graph_url()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=body, headers=headers)
        response.raise_for_status()
        return response.json()


def _wamid_from(response: dict[str, Any] | None) -> str | None:
    if not response:
        return None
    messages = response.get("messages") or []
    if not messages:
        return None
    return messages[0].get("id")


def _build_interactive_buttons_body(
    *,
    to: str,
    text: str,
    buttons: list[dict[str, str]],
) -> dict[str, Any]:
    """Construct an interactive button message (max 3 reply buttons).

    Each button dict must have ``id`` and ``title``; we trim defensively to
    Meta's limits so a too-long title doesn't fail the whole send. Buttons
    beyond the third are dropped — they wouldn't render anyway."""
    bounded = buttons[:_MAX_BUTTONS]
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": _normalize_to(to),
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": text},
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {
                            "id": str(b["id"])[:_MAX_BUTTON_ID_CHARS],
                            "title": str(b["title"])[:_MAX_BUTTON_TITLE_CHARS],
                        },
                    }
                    for b in bounded
                ]
            },
        },
    }


def _build_interactive_list_body(
    *,
    to: str,
    text: str,
    rows: list[dict[str, str]],
    button_label: str = "Select",
    section_title: str = "Options",
) -> dict[str, Any]:
    """Construct an interactive list message (max 10 rows, single section).

    Defensively trims oversized fields to Meta's limits so a stray title or
    description doesn't fail the whole send. Rows beyond the 10th are dropped.
    """
    bounded = rows[:_MAX_LIST_ROWS]
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": _normalize_to(to),
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": text},
            "action": {
                "button": (button_label or "Select")[:_MAX_LIST_BUTTON_CHARS],
                "sections": [
                    {
                        "title": (section_title or "Options")[
                            :_MAX_LIST_SECTION_TITLE_CHARS
                        ],
                        "rows": [
                            {
                                "id": str(r["id"])[:_MAX_LIST_ROW_ID_CHARS],
                                "title": str(r["title"])[
                                    :_MAX_LIST_ROW_TITLE_CHARS
                                ],
                                **(
                                    {
                                        "description": str(r["description"])[
                                            :_MAX_LIST_ROW_DESC_CHARS
                                        ]
                                    }
                                    if r.get("description")
                                    else {}
                                ),
                            }
                            for r in bounded
                        ],
                    }
                ],
            },
        },
    }


async def send_interactive_list(
    *,
    to: str,
    text: str,
    rows: list[dict[str, str]],
    button_label: str = "Select",
    section_title: str = "Options",
) -> MetaSendResult:
    """Send an interactive list message (up to 10 tappable rows).

    Freeform-only — only deliverable inside the 24h customer service window.
    Outside-window scheduling presentations should fall back to plain text or
    template variants (no list-message-in-template support today)."""
    if not rows:
        return await send_freeform(to=to, text=text)
    body = _build_interactive_list_body(
        to=to,
        text=text,
        rows=rows,
        button_label=button_label,
        section_title=section_title,
    )
    if _is_dry_run():
        return MetaSendResult(
            accepted=True,
            payload_kind="interactive_list",
            dry_run=True,
            wamid=None,
            response=None,
        )
    try:
        response = await _post_to_meta(body)
    except httpx.HTTPError as exc:
        log.warning("interactive list send failed: %s", exc)
        return MetaSendResult(
            accepted=False,
            payload_kind="interactive_list",
            dry_run=False,
            wamid=None,
            response=None,
            error=f"http_error:{type(exc).__name__}:{exc}",
        )
    return MetaSendResult(
        accepted=True,
        payload_kind="interactive_list",
        dry_run=False,
        wamid=_wamid_from(response),
        response=response,
    )


async def send_interactive_buttons(
    *,
    to: str,
    text: str,
    buttons: list[dict[str, str]],
) -> MetaSendResult:
    """Send an interactive button message (Cancel/Reschedule/etc).

    Freeform-only — only deliverable inside the 24h customer service window.
    Outside-window sends should use a template with template-button components
    (separate Meta-approved template, not implemented here)."""
    if not buttons:
        # Safety net — caller should have routed to send_freeform.
        return await send_freeform(to=to, text=text)
    body = _build_interactive_buttons_body(to=to, text=text, buttons=buttons)
    if _is_dry_run():
        return MetaSendResult(
            accepted=True,
            payload_kind="interactive_buttons",
            dry_run=True,
            wamid=None,
            response=None,
        )
    try:
        response = await _post_to_meta(body)
    except httpx.HTTPError as exc:
        log.warning("interactive button send failed: %s", exc)
        return MetaSendResult(
            accepted=False,
            payload_kind="interactive_buttons",
            dry_run=False,
            wamid=None,
            response=None,
            error=f"http_error:{type(exc).__name__}:{exc}",
        )
    return MetaSendResult(
        accepted=True,
        payload_kind="interactive_buttons",
        dry_run=False,
        wamid=_wamid_from(response),
        response=response,
    )


async def send_freeform(*, to: str, text: str) -> MetaSendResult:
    body = _build_freeform_body(to=to, text=text)
    if _is_dry_run():
        return MetaSendResult(
            accepted=True,
            payload_kind="freeform",
            dry_run=True,
            wamid=None,
            response=None,
        )
    try:
        response = await _post_to_meta(body)
    except httpx.HTTPError as exc:
        log.warning("freeform send failed: %s", exc)
        return MetaSendResult(
            accepted=False,
            payload_kind="freeform",
            dry_run=False,
            wamid=None,
            response=None,
            error=f"http_error:{type(exc).__name__}:{exc}",
        )
    return MetaSendResult(
        accepted=True,
        payload_kind="freeform",
        dry_run=False,
        wamid=_wamid_from(response),
        response=response,
    )


async def send_template(
    *,
    to: str,
    template_name: str,
    template_params: dict[str, str | int | float | bool] | None = None,
    language: str | None = None,
    button_payloads: list[dict[str, str]] | None = None,
) -> MetaSendResult:
    body = _build_template_body(
        to=to,
        template_name=template_name,
        template_params=template_params,
        language=language,
        button_payloads=button_payloads,
    )
    if _is_dry_run():
        return MetaSendResult(
            accepted=True,
            payload_kind="template",
            dry_run=True,
            wamid=None,
            response=None,
        )
    try:
        response = await _post_to_meta(body)
    except httpx.HTTPError as exc:
        log.warning("template send failed: %s", exc)
        return MetaSendResult(
            accepted=False,
            payload_kind="template",
            dry_run=False,
            wamid=None,
            response=None,
            error=f"http_error:{type(exc).__name__}:{exc}",
        )
    return MetaSendResult(
        accepted=True,
        payload_kind="template",
        dry_run=False,
        wamid=_wamid_from(response),
        response=response,
    )

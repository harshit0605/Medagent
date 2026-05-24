"""Shared-secret auth for the internal WhatsApp gateway.

The gateway's ``/send`` (and the other state-changing / log endpoints) must not
be callable by anything that can merely reach the port — an unauthenticated
``/send`` lets a caller send WhatsApp messages as us. The gateway validates a
shared ``GATEWAY_API_KEY``; the server-to-server callers (orchestrator +
scheduler) attach it via :func:`gateway_auth_headers`.

Both sides read ``GATEWAY_API_KEY`` from the environment. When it's unset the
helper returns no header and the gateway runs unauthenticated (only allowed
when ``ALLOW_UNAUTHENTICATED=1`` — dev / tests), so existing callers keep
working until the key is configured everywhere.
"""

from __future__ import annotations

import os


def gateway_auth_headers() -> dict[str, str]:
    """The ``x-api-key`` header for a server-to-server gateway call, or an
    empty dict when ``GATEWAY_API_KEY`` is not configured."""
    key = os.getenv("GATEWAY_API_KEY", "")
    return {"x-api-key": key} if key else {}

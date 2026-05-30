"""Shared liveness / readiness probes for all four services.

Kubernetes-style split:
  * **Liveness** (``/health``): "is the process up?" — a trivial 200. Never
    touches dependencies; a slow DB must NOT make the orchestrator look dead
    and get killed. This stays the auth-exempt endpoint Coolify/Traefik hit
    for "is the container alive".
  * **Readiness** (``/health/ready``): "can this replica serve traffic right
    now?" — pings the DB, confirms the Fernet key is loadable, and (when the
    LLM is enabled) confirms the API key is present. A failing readiness probe
    returns 503 so a load balancer routes around the degraded replica without
    killing it.

The readiness checks are all fast and side-effect-free. The DB ping is a
``SELECT 1`` with a short timeout; we never block the probe on a slow query.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any


async def _db_ping(timeout: float = 2.0) -> dict[str, Any]:
    """SELECT 1 against the pool. Bounded so a wedged DB can't hang the probe."""
    try:
        from sqlalchemy import text

        from app.db.session import get_sessionmaker

        async def _run() -> None:
            SessionLocal = get_sessionmaker()
            async with SessionLocal() as db:
                await db.execute(text("SELECT 1"))

        await asyncio.wait_for(_run(), timeout=timeout)
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001 — any failure is "not ready"
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}


def _fernet_check() -> dict[str, Any]:
    """Confirm the OAuth-token encryption key is loadable. A missing /
    malformed key means we can't decrypt doctors' calendar tokens — the
    replica is effectively broken for calendar flows even if it boots."""
    if not os.getenv("MEDAGENT_FERNET_KEY"):
        # No key configured at all — calendar OAuth is simply disabled, which
        # is a valid deployment. Report ``skipped`` rather than failing.
        return {"ok": True, "skipped": "MEDAGENT_FERNET_KEY unset"}
    try:
        from app.security import crypto

        crypto._get_fernet()
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}


def _llm_check() -> dict[str, Any]:
    """When the LLM is enabled, confirm an API key is present. We don't make a
    network call — just that the config is coherent so we don't silently fall
    back to the deterministic path in prod."""
    if os.getenv("LLM_ENABLED", "1") == "0":
        return {"ok": True, "skipped": "LLM_ENABLED=0"}
    if not os.getenv("OPENAI_API_KEY"):
        return {"ok": False, "error": "LLM enabled but OPENAI_API_KEY unset"}
    return {"ok": True}


async def readiness_report(
    *, check_db: bool = True, check_fernet: bool = True, check_llm: bool = True
) -> tuple[bool, dict[str, Any]]:
    """Run the readiness checks and return ``(ready, report)``.

    Services opt out of individual checks via the kwargs — e.g. the gateway
    doesn't use the LLM, so it passes ``check_llm=False``.
    """
    checks: dict[str, Any] = {}
    if check_db:
        checks["db"] = await _db_ping()
    if check_fernet:
        checks["fernet"] = _fernet_check()
    if check_llm:
        checks["llm"] = _llm_check()
    ready = all(c.get("ok", False) for c in checks.values())
    return ready, {"status": "ready" if ready else "not_ready", "checks": checks}


__all__ = ["readiness_report"]

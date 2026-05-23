"""Centralised LLM-call tracker.

Wraps OpenAI calls so every code path that hits the API logs a
row to ``llm_call_logs``. Without this, cost + latency
analytics would be partial — half the bot's spend would be
invisible because half the call sites would lack instrumentation.

**Context plumbing via contextvars.** The naive approach would
have every LLM call site take a ``session`` + ``patient_id`` so
the tracker could write a properly-attributed row. But that
threads two extra args through ``classify_inbound``,
``compose_reply``, ``generate_recap``, the ReAct loop's tool
loop, etc. — many of which have no other reason to know.

Instead, the request entry point (``/route``) sets a
contextvar carrying the session + patient_id + message_id, and
every ``track_llm_call`` reads from it. Async tasks inherit
the parent's context, so the propagation is automatic — no
function-signature changes.

Usage::

    # At request entry (e.g. /route handler):
    set_llm_tracking_context(
        session=db, patient_id=patient_phone,
        message_id=message_id,
    )

    # At any LLM call site (no extra args needed):
    async with track_llm_call(
        call_kind="compose_reply", model=llm.model,
    ) as tracker:
        completion = await client.chat.completions.parse(...)
        tracker.set_completion(completion)

If the LLM call raises, the context manager records the
exception type + message and re-raises. Latency is recorded
regardless. If no context is set (e.g. unit tests, scheduler
sweeps), tracking is a no-op — better than forcing every
caller to build one solely for telemetry.

Pricing config + cost computation lives at the bottom of this
module; rates are stored as integer USD-per-1M-tokens micros so
the runtime arithmetic stays exact.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import llm_calls as llm_calls_repo

log = logging.getLogger(__name__)


# ---- Pricing -------------------------------------------------------------

# Per-model rates as USD-per-1M-tokens × 10⁶ = USD-micros per
# 1M-tokens. Computing cost as ``tokens × rate // 1M`` preserves
# fractional cents on cheap models that would otherwise round to
# zero per-token.
#
# Source: OpenAI public pricing (May 2025 snapshot). Update when
# prices change. Unknown models return cost=0 — tokens still
# tracked, the cost line just shows $0.00 as a "rate not
# configured" signal.
_RATE_PER_MILLION_MICROS: dict[str, dict[str, int]] = {
    # gpt-4o-mini: $0.15 / 1M prompt, $0.60 / 1M completion.
    "gpt-4o-mini": {"prompt": 150_000, "completion": 600_000},
    # gpt-4o: $2.50 / 1M prompt, $10.00 / 1M completion.
    "gpt-4o": {"prompt": 2_500_000, "completion": 10_000_000},
    # gpt-4-turbo: $10 / 1M, $30 / 1M.
    "gpt-4-turbo": {"prompt": 10_000_000, "completion": 30_000_000},
    # gpt-3.5-turbo: $0.50 / 1M, $1.50 / 1M.
    "gpt-3.5-turbo": {"prompt": 500_000, "completion": 1_500_000},
}


def compute_cost_micros(
    *, model: str, prompt_tokens: int, completion_tokens: int
) -> int:
    """Compute the cost of one OpenAI call in USD micros (10⁻⁶
    USD) given tokens + model. Returns 0 for unknown models.

    Integer math: ``tokens × rate_per_million_micros // 1_000_000``.
    Multiplying tokens BEFORE the divide preserves cents on cheap
    models (gpt-4o-mini's 0.15-micros-per-token would round to
    zero per-token; the multiply-first dance preserves them)."""
    rates = _RATE_PER_MILLION_MICROS.get(model)
    if rates is None:
        return 0
    prompt_cost = (prompt_tokens * rates["prompt"]) // 1_000_000
    completion_cost = (
        completion_tokens * rates["completion"]
    ) // 1_000_000
    return prompt_cost + completion_cost


# ---- Request-scoped context (set by /route, read by tracker) -------------


@dataclass
class LlmTrackingContext:
    """The ambient context the tracker writes against. Set at
    /route entry; reset at exit. Async tasks inherit the parent
    context so handlers + workflows + LLM calls all see the
    same patient_id without passing it through every call.
    """

    session: AsyncSession | None = None
    patient_id: str | None = None
    message_id: str | None = None


_current_context: ContextVar[LlmTrackingContext] = ContextVar(
    "llm_tracking_context", default=LlmTrackingContext()
)


def set_llm_tracking_context(
    *,
    session: AsyncSession | None = None,
    patient_id: str | None = None,
    message_id: str | None = None,
) -> None:
    """Replace the current request's tracking context.
    Idempotent — calling again overrides; clearing is just
    ``set_llm_tracking_context()`` with no args."""
    _current_context.set(
        LlmTrackingContext(
            session=session,
            patient_id=patient_id,
            message_id=message_id,
        )
    )


def get_llm_tracking_context() -> LlmTrackingContext:
    """Read the current ambient context. Returns a default
    (all-None) value if the request entry never set one — the
    tracker treats that as "no-op tracking", not an error."""
    return _current_context.get()


# ---- Tracker context manager ---------------------------------------------


@dataclass
class _Tracker:
    """Mutable state passed to the caller inside
    ``track_llm_call``. The caller stores the OpenAI completion
    on success; the wrapper extracts ``.usage`` at exit."""

    completion: Any | None = None
    explicit_tokens: dict[str, int] | None = field(default=None)

    def set_completion(self, completion: Any) -> None:
        """Stash the OpenAI completion so the wrapper can read
        its ``.usage`` at exit."""
        self.completion = completion

    def set_tokens(
        self, *, prompt_tokens: int, completion_tokens: int
    ) -> None:
        """Override token capture for code paths without a
        single completion object (multi-turn, batched calls)."""
        self.explicit_tokens = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        }


def _extract_usage(completion: Any) -> tuple[int, int, int]:
    """Pull (prompt, completion, total) tokens from an OpenAI
    completion. Defensive on missing fields."""
    if completion is None:
        return 0, 0, 0
    usage = getattr(completion, "usage", None)
    if usage is None:
        return 0, 0, 0
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion_n = int(getattr(usage, "completion_tokens", 0) or 0)
    total = int(
        getattr(usage, "total_tokens", prompt + completion_n) or 0
    )
    return prompt, completion_n, total


@asynccontextmanager
async def track_llm_call(
    *,
    call_kind: str,
    model: str,
    session: AsyncSession | None = None,
    patient_id: str | None = None,
    message_id: str | None = None,
) -> AsyncIterator[_Tracker]:
    """Wrap an OpenAI call with token + latency tracking.

    Reads ``session`` / ``patient_id`` / ``message_id`` from the
    ambient ``LlmTrackingContext`` if not passed explicitly.
    Pass them explicitly to override (e.g. in a sweep that has
    its own session but no /route entry).

    On success the caller stores the completion via
    ``tracker.set_completion(completion)`` so the wrapper can
    read ``.usage``. On failure the wrapper catches the
    exception, records ``error="<class>: <msg>"``, re-raises.

    Tracking is best-effort: a failure persisting the row is
    logged + swallowed so it never breaks the LLM call's outcome.
    """
    ctx = get_llm_tracking_context()
    effective_session = session or ctx.session
    effective_patient_id = patient_id or ctx.patient_id
    effective_message_id = message_id or ctx.message_id

    tracker = _Tracker()
    started_at = time.monotonic()
    error_str: str | None = None
    try:
        yield tracker
    except BaseException as exc:  # noqa: BLE001 — capture + re-raise
        error_str = f"{type(exc).__name__}: {str(exc)[:200]}"
        raise
    finally:
        if effective_session is None:
            # No session means no DB to write against. Common in
            # tests + offline dev. Log so the call is at least
            # visible in stdout.
            log.debug(
                "llm call (untracked): kind=%s model=%s",
                call_kind,
                model,
            )
        else:
            latency_ms = int((time.monotonic() - started_at) * 1000)

            if tracker.explicit_tokens is not None:
                prompt = tracker.explicit_tokens["prompt_tokens"]
                completion_n = tracker.explicit_tokens["completion_tokens"]
                total = prompt + completion_n
            else:
                prompt, completion_n, total = _extract_usage(
                    tracker.completion
                )

            cost = compute_cost_micros(
                model=model,
                prompt_tokens=prompt,
                completion_tokens=completion_n,
            )

            try:
                await llm_calls_repo.record(
                    effective_session,
                    call_kind=call_kind,
                    model=model,
                    prompt_tokens=prompt,
                    completion_tokens=completion_n,
                    total_tokens=total,
                    latency_ms=latency_ms,
                    cost_usd_micros=cost,
                    patient_id=effective_patient_id,
                    message_id=effective_message_id,
                    error=error_str,
                )
            except Exception:  # noqa: BLE001 — best-effort
                log.exception(
                    "llm tracking persist failed; continuing"
                )

"""Simple in-process circuit breaker.

Tracks consecutive failures against an external dependency (LLM, sweep, etc).
When the consecutive-failure count crosses ``threshold`` the breaker OPENS:
``is_open()`` returns True for ``reset_after_seconds`` and callers should
short-circuit instead of hitting the failing dependency.

After ``reset_after_seconds`` the breaker enters HALF-OPEN: the next
``is_open()`` call returns False so a single probe goes through. On success
the breaker closes (counter reset); on failure it re-opens for another
window (counter NOT reset, so the back-off effectively elongates).

Scope: in-process. Each worker / replica maintains its own state. Acceptable
for the use cases here:
  * LLM dependency degradation is visible across replicas (Anthropic /
    OpenAI is shared), so all replicas trip independently within seconds.
  * Sweep failures are usually deterministic on the bad row, so a replica
    that's running the sweep will see the same failure regardless of the
    others. The next sweep tick on each replica trips its own breaker.

For DB-coordinated coordination across replicas (e.g. a hard global pause),
escalate to a flag in the database — out of scope here.

Thread-safety: the counter mutations are tiny and idempotent enough that the
race between threads / coroutines is harmless (worst case: one extra call
slips through right as the threshold is crossed). Async-lock-free for
simplicity.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class CircuitBreaker:
    """A simple closed/open/half-open circuit breaker.

    Use::

        BREAKER = CircuitBreaker(name="llm", threshold=5, reset_after_seconds=120)

        if BREAKER.is_open():
            return None  # fall back / fail fast
        try:
            result = await call_dependency()
            BREAKER.record_success()
            return result
        except Exception:
            BREAKER.record_failure()
            raise  # or return fallback
    """

    name: str
    threshold: int = 5
    reset_after_seconds: float = 120.0
    _consecutive_failures: int = 0
    _open_until_monotonic: float | None = None

    def is_open(self) -> bool:
        """True iff the breaker is currently rejecting calls.

        Transitions out of OPEN automatically when the reset window elapses:
        the first ``is_open()`` call past the window returns False (HALF-OPEN
        probe). If the probe call later records a failure the breaker opens
        again; if it records a success the counter resets to zero.
        """
        if self._open_until_monotonic is None:
            return False
        if time.monotonic() >= self._open_until_monotonic:
            # Half-open: allow one probe through. Reset the deadline so
            # subsequent is_open() calls before the probe completes still
            # let through (no stampede protection — keep it simple).
            self._open_until_monotonic = None
            return False
        return True

    def record_success(self) -> None:
        """Reset the failure counter and close the breaker."""
        self._consecutive_failures = 0
        self._open_until_monotonic = None

    def record_failure(self) -> None:
        """Tick the failure counter; open the breaker if the threshold is met."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.threshold:
            self._open_until_monotonic = (
                time.monotonic() + self.reset_after_seconds
            )

    def reset(self) -> None:
        """Force-close the breaker (for test cleanup or operator override)."""
        self._consecutive_failures = 0
        self._open_until_monotonic = None

    def snapshot(self) -> dict[str, object]:
        """Diagnostic snapshot — safe to log / surface in /health."""
        open_for_more = None
        if self._open_until_monotonic is not None:
            open_for_more = max(
                0.0, self._open_until_monotonic - time.monotonic()
            )
        return {
            "name": self.name,
            "consecutive_failures": self._consecutive_failures,
            "open": self.is_open(),
            "open_for_seconds_more": open_for_more,
            "threshold": self.threshold,
            "reset_after_seconds": self.reset_after_seconds,
        }


__all__ = ["CircuitBreaker"]

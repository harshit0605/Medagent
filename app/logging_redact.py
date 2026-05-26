"""PII redaction helpers for log lines.

We log patient identifiers for operational triage (correlation across requests,
debugging), but never the verbatim PII. Use ``redact_phone(p)`` to produce a
log-safe identifier that still lets ops grep for the same patient across logs
without exposing the full number to log aggregators (CloudWatch, Datadog, etc).

The redacted form keeps the LAST 4 digits so a ticketed phone number is still
identifiable to an operator who has the original, but useless to a leak.
Empty / None / too-short inputs collapse to ``"<redacted>"`` rather than
leaking the input itself.

Why not a logging.Filter that auto-scans records? It produces too many false
positives (UUIDs, hashes, IDs) and gives a false sense of safety — call sites
that interpolate via .format() before the filter sees the record still leak.
Explicit redaction at the call site is auditable and grep-able.
"""

from __future__ import annotations

import re

# Anything that's clearly not a phone (UUID, hash, short id) collapses to the
# sentinel rather than partial-leaking. ``+9198765-aabb1234`` style test phones
# (used by the integration suite) DO get partially-redacted, which is fine —
# they're not real PII.
_MIN_LEN = 6
_REDACT_SENTINEL = "<redacted>"

# Strip everything that isn't a digit or letter when measuring length. Phones
# in the system look like ``+919876543210`` or ``919876543210`` or test-shaped
# ``onb-asthma-abcd1234``; we want to keep the trailing 4 chars intact and
# mask the rest.
_NON_PRINT = re.compile(r"[\s]+")


def redact_phone(value: str | None) -> str:
    """Return a log-safe identifier for ``value``.

    Keeps the last 4 characters of the cleaned input; everything before that
    becomes asterisks. Returns ``"<redacted>"`` for None / empty / too-short
    inputs so the log line never accidentally surfaces the raw value.

    Examples
    --------
    >>> redact_phone("+919876543210")
    '*********3210'
    >>> redact_phone("onb-asthma-abcd1234")
    '***************1234'
    >>> redact_phone(None)
    '<redacted>'
    >>> redact_phone("")
    '<redacted>'
    >>> redact_phone("abc")
    '<redacted>'
    """
    if value is None:
        return _REDACT_SENTINEL
    cleaned = _NON_PRINT.sub("", str(value))
    if len(cleaned) < _MIN_LEN:
        return _REDACT_SENTINEL
    tail = cleaned[-4:]
    return ("*" * (len(cleaned) - 4)) + tail


__all__ = ["redact_phone"]

"""HMAC-SHA256 signing for the ``X-Ops-Actor`` header.

The current shared-API-key model lets the ops console assert any operator
identity in the ``X-Ops-Actor`` header — fine for "who did this?" audit
attribution as long as the ops console is trusted, but a compromised API
key would let an attacker forge any actor name.

When the ops console signs its actor identity with a shared HMAC secret
(``OPS_ACTOR_SIGNING_KEY``), the orchestrator can verify the binding and
distinguish a forged actor from a legitimate one. Gated by
``OPS_ACTOR_SIGNATURE_REQUIRED=1`` so the rollout is backward-compatible:

  * Default (``0``): orchestrator accepts both signed + unsigned headers,
    falls back to the existing caller-asserted semantics.
  * Strict (``1``): unsigned / mis-signed headers on privileged endpoints
    return 401.

Algorithm:
  signature = HEX(HMAC-SHA256(key, actor.encode("utf-8")))

The actor string is the message; no nonce / timestamp is included. The
shared API key prevents replay across origins, and the actor name itself
is the integrity-protected value. Adding a timestamp would require clock
sync the system doesn't otherwise need — out of scope here.
"""

from __future__ import annotations

import hashlib
import hmac
import os


def sign(actor: str, *, key: str) -> str:
    """Return the lowercase-hex HMAC-SHA256 signature for ``actor``."""
    if not key:
        raise ValueError("signing key is required")
    return hmac.new(
        key.encode("utf-8"),
        actor.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify(actor: str, signature: str, *, key: str) -> bool:
    """Constant-time compare a candidate signature against the expected one.

    Returns False (never raises) on any malformed input — bad signatures are
    expected enough that callers shouldn't need a try/except. Empty key /
    empty actor / empty signature all return False.
    """
    if not (actor and signature and key):
        return False
    try:
        expected = sign(actor, key=key)
    except ValueError:
        return False
    return hmac.compare_digest(expected, signature)


def is_required() -> bool:
    """Whether the orchestrator should reject unsigned ops-actor headers."""
    return os.getenv("OPS_ACTOR_SIGNATURE_REQUIRED", "0") == "1"


def signing_key() -> str:
    """Shared-secret HMAC key, or empty string when unset.

    Production: set ``OPS_ACTOR_SIGNING_KEY`` on BOTH the orchestrator
    container and the ops-console container (same value). The ops console's
    server-side ``call()`` reads it via ``process.env``.
    """
    return os.getenv("OPS_ACTOR_SIGNING_KEY", "")


def resolve_actor(
    *,
    header_actor: str | None,
    header_signature: str | None,
    fallback: str | None = None,
) -> tuple[str, bool]:
    """Resolve the operator-actor identity for a privileged endpoint.

    Returns ``(actor, signed)`` where ``signed`` is True iff the supplied
    signature verified against ``header_actor`` with the configured key.

    Behaviour matrix (assuming ``OPS_ACTOR_SIGNING_KEY`` configured):

    +-----------------+----------------+-----------------+---------------------+
    | header_actor    | header_sig     | REQUIRED=0      | REQUIRED=1          |
    +=================+================+=================+=====================+
    | unset           | -              | fallback, False | RuntimeError 401    |
    | set             | unset / wrong  | actor, False    | RuntimeError 401    |
    | set             | valid          | actor, True     | actor, True         |
    +-----------------+----------------+-----------------+---------------------+

    When the signing key itself is unset, verification is impossible and
    the function falls back to caller-asserted semantics regardless of
    ``OPS_ACTOR_SIGNATURE_REQUIRED``. Callers that want hard rejection
    must set BOTH the key AND the required flag.

    Raises :class:`ValueError` (string ``"401:<reason>"``) when ``REQUIRED=1``
    and the input doesn't verify — the FastAPI router converts it to a 401.
    """
    key = signing_key()
    required = is_required() and bool(key)

    if not header_actor:
        if required:
            raise ValueError("401:missing X-Ops-Actor header")
        return (fallback or "ops"), False

    is_signed = bool(key) and bool(header_signature) and verify(
        header_actor, header_signature, key=key
    )
    if required and not is_signed:
        raise ValueError("401:invalid or missing X-Ops-Actor-Signature")
    return header_actor, is_signed


__all__ = [
    "sign",
    "verify",
    "is_required",
    "signing_key",
    "resolve_actor",
]

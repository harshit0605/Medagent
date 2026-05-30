"""Symmetric encryption for at-rest secrets (e.g. OAuth refresh tokens).

Uses Fernet (AES-128-CBC + HMAC-SHA256), wrapped in ``MultiFernet`` so keys
can be ROTATED without a flag-day re-encrypt:

  * ``MEDAGENT_FERNET_KEY`` is the PRIMARY key — every new ``encrypt`` uses it.
  * ``MEDAGENT_FERNET_KEYS_OLD`` (optional, comma-separated) holds previous
    keys kept around ONLY so existing ciphertexts still ``decrypt`` during a
    rotation. MultiFernet tries the primary first, then each old key.

Generate a key with::

    uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Rotation playbook (see docs/SECRETS_ROTATION.md):
  1. Generate a new key. Set ``MEDAGENT_FERNET_KEY=<new>`` and move the current
     key into ``MEDAGENT_FERNET_KEYS_OLD=<old>``. Deploy. New writes use the
     new key; old ciphertexts still decrypt via the old key.
  2. Re-encrypt stored ciphertexts to the new primary with
     :func:`rotate_token` (e.g. a one-off backfill over doctor OAuth tokens).
  3. Once nothing references the old key, drop ``MEDAGENT_FERNET_KEYS_OLD``.

With only ``MEDAGENT_FERNET_KEY`` set, MultiFernet wraps a single key and
behaves identically to the previous single-Fernet implementation.
"""

from __future__ import annotations

import os
from threading import Lock

from cryptography.fernet import Fernet, InvalidToken, MultiFernet


_multi: MultiFernet | None = None
_lock = Lock()


def _build_multifernet() -> MultiFernet:
    primary = os.getenv("MEDAGENT_FERNET_KEY")
    if not primary:
        raise RuntimeError(
            "MEDAGENT_FERNET_KEY is not set — generate one via "
            "Fernet.generate_key().decode() and put it in .env"
        )
    keys = [primary]
    old = os.getenv("MEDAGENT_FERNET_KEYS_OLD", "")
    keys.extend(k.strip() for k in old.split(",") if k.strip())
    fernets = [Fernet(k.encode() if isinstance(k, str) else k) for k in keys]
    return MultiFernet(fernets)


def _get_fernet() -> MultiFernet:
    global _multi
    if _multi is None:
        with _lock:
            if _multi is None:
                _multi = _build_multifernet()
    return _multi


def reset_crypto_for_tests() -> None:
    """Drop the cached MultiFernet so a test that swaps the key env vars gets
    a freshly-built bundle on the next call."""
    global _multi
    with _lock:
        _multi = None


def encrypt(plaintext: str) -> str:
    """Encrypt a UTF-8 string with the PRIMARY key. Returns URL-safe ASCII."""
    if plaintext is None:
        raise ValueError("plaintext is required")
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    """Inverse of :func:`encrypt`. Tries the primary then each old key; raises
    ``InvalidToken`` on tamper / no-matching-key."""
    if ciphertext is None:
        raise ValueError("ciphertext is required")
    return _get_fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")


def rotate_token(ciphertext: str) -> str:
    """Re-encrypt an existing ciphertext under the PRIMARY key without exposing
    the plaintext. Use during step 2 of the rotation playbook to migrate stored
    ciphertexts off an old key. No-op-equivalent if it's already primary."""
    if ciphertext is None:
        raise ValueError("ciphertext is required")
    return _get_fernet().rotate(ciphertext.encode("ascii")).decode("ascii")


__all__ = ["encrypt", "decrypt", "rotate_token", "reset_crypto_for_tests", "InvalidToken"]

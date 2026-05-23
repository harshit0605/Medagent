"""Symmetric encryption for at-rest secrets (e.g. OAuth refresh tokens).

Uses Fernet (AES-128-CBC + HMAC-SHA256). The key is read from
``MEDAGENT_FERNET_KEY`` and must be a 32-byte URL-safe base64 string —
generate one with::

    uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Rotation is intentionally out of scope for v1; a future iteration can layer
in a multi-key Fernet bundle if needed.
"""

from __future__ import annotations

import os
from threading import Lock

from cryptography.fernet import Fernet, InvalidToken


_fernet: Fernet | None = None
_lock = Lock()


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        with _lock:
            if _fernet is None:
                key = os.getenv("MEDAGENT_FERNET_KEY")
                if not key:
                    raise RuntimeError(
                        "MEDAGENT_FERNET_KEY is not set — generate one via "
                        "Fernet.generate_key().decode() and put it in .env"
                    )
                _fernet = Fernet(key.encode() if isinstance(key, str) else key)
    return _fernet


def encrypt(plaintext: str) -> str:
    """Encrypt a UTF-8 string. Returns the Fernet ciphertext as a URL-safe ASCII string."""
    if plaintext is None:
        raise ValueError("plaintext is required")
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    """Inverse of :func:`encrypt`. Raises ``InvalidToken`` on tamper / wrong key."""
    if ciphertext is None:
        raise ValueError("ciphertext is required")
    return _get_fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")


__all__ = ["encrypt", "decrypt", "InvalidToken"]

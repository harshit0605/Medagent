"""Round-trip tests for the Fernet at-rest encryption helper."""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

import pytest


@pytest.fixture(autouse=True)
def _scoped_fernet_key(monkeypatch):
    monkeypatch.setenv("MEDAGENT_FERNET_KEY", Fernet.generate_key().decode())
    # Reset the singleton between tests so each test gets the freshly-set key.
    import app.security.crypto as crypto_module

    crypto_module._fernet = None
    yield
    crypto_module._fernet = None


def test_round_trip_preserves_value():
    from app.security.crypto import decrypt, encrypt

    secret = "1//abc-refresh-token-XYZ"
    cipher = encrypt(secret)
    assert cipher != secret  # not plaintext
    assert decrypt(cipher) == secret


def test_distinct_calls_produce_distinct_ciphertexts():
    from app.security.crypto import encrypt

    a = encrypt("same plaintext")
    b = encrypt("same plaintext")
    # Fernet includes a random IV per call so two calls of the same plaintext
    # yield different ciphertexts. This protects against pattern leaks.
    assert a != b


def test_decrypt_with_wrong_key_raises_invalid_token(monkeypatch):
    from app.security.crypto import encrypt
    import app.security.crypto as crypto_module

    cipher = encrypt("secret")

    monkeypatch.setenv("MEDAGENT_FERNET_KEY", Fernet.generate_key().decode())
    crypto_module._fernet = None  # force re-init with the new key

    from app.security.crypto import decrypt

    with pytest.raises(InvalidToken):
        decrypt(cipher)


def test_missing_key_raises_runtime_error(monkeypatch):
    monkeypatch.delenv("MEDAGENT_FERNET_KEY", raising=False)
    import app.security.crypto as crypto_module

    crypto_module._fernet = None

    from app.security.crypto import encrypt

    with pytest.raises(RuntimeError):
        encrypt("anything")

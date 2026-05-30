"""Round-trip tests for the Fernet at-rest encryption helper."""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

import pytest


@pytest.fixture(autouse=True)
def _scoped_fernet_key(monkeypatch):
    monkeypatch.setenv("MEDAGENT_FERNET_KEY", Fernet.generate_key().decode())
    monkeypatch.delenv("MEDAGENT_FERNET_KEYS_OLD", raising=False)
    # Reset the singleton between tests so each test gets the freshly-set key.
    import app.security.crypto as crypto_module

    crypto_module.reset_crypto_for_tests()
    yield
    crypto_module.reset_crypto_for_tests()


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
    monkeypatch.delenv("MEDAGENT_FERNET_KEYS_OLD", raising=False)
    crypto_module.reset_crypto_for_tests()  # force re-init with the new key

    from app.security.crypto import decrypt

    with pytest.raises(InvalidToken):
        decrypt(cipher)


def test_missing_key_raises_runtime_error(monkeypatch):
    monkeypatch.delenv("MEDAGENT_FERNET_KEY", raising=False)
    import app.security.crypto as crypto_module

    crypto_module.reset_crypto_for_tests()

    from app.security.crypto import encrypt

    with pytest.raises(RuntimeError):
        encrypt("anything")


def test_old_key_still_decrypts_during_rotation(monkeypatch):
    """Ciphertext written under the old key must still decrypt after the
    primary key rotates, as long as the old key is in MEDAGENT_FERNET_KEYS_OLD."""
    import app.security.crypto as crypto_module

    old_key = Fernet.generate_key().decode()
    monkeypatch.setenv("MEDAGENT_FERNET_KEY", old_key)
    crypto_module.reset_crypto_for_tests()
    from app.security.crypto import encrypt

    cipher = encrypt("rotate-me")

    # Rotate: new primary, old key demoted to KEYS_OLD.
    new_key = Fernet.generate_key().decode()
    monkeypatch.setenv("MEDAGENT_FERNET_KEY", new_key)
    monkeypatch.setenv("MEDAGENT_FERNET_KEYS_OLD", old_key)
    crypto_module.reset_crypto_for_tests()

    from app.security.crypto import decrypt, rotate_token

    # Old ciphertext still decrypts via the demoted key.
    assert decrypt(cipher) == "rotate-me"

    # Re-encrypt to the new primary; drop the old key — still decrypts.
    rotated = rotate_token(cipher)
    monkeypatch.delenv("MEDAGENT_FERNET_KEYS_OLD", raising=False)
    crypto_module.reset_crypto_for_tests()
    assert decrypt(rotated) == "rotate-me"


def test_rotated_ciphertext_fails_without_old_key(monkeypatch):
    """Sanity: once the old key is gone, an UN-rotated old ciphertext can no
    longer be decrypted — proving rotate_token() is actually required."""
    import app.security.crypto as crypto_module

    old_key = Fernet.generate_key().decode()
    monkeypatch.setenv("MEDAGENT_FERNET_KEY", old_key)
    crypto_module.reset_crypto_for_tests()
    from app.security.crypto import encrypt

    cipher = encrypt("orphan")

    new_key = Fernet.generate_key().decode()
    monkeypatch.setenv("MEDAGENT_FERNET_KEY", new_key)
    monkeypatch.delenv("MEDAGENT_FERNET_KEYS_OLD", raising=False)
    crypto_module.reset_crypto_for_tests()

    from app.security.crypto import decrypt

    with pytest.raises(InvalidToken):
        decrypt(cipher)

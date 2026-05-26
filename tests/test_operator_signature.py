"""Unit tests for the operator-actor HMAC signing primitive."""

from __future__ import annotations

from app.operator_signature import sign, verify


_KEY = "secret-key-for-tests"


def test_sign_returns_hex_string_of_expected_length():
    sig = sign("alice@clinic", key=_KEY)
    assert len(sig) == 64  # SHA-256 hex digest
    assert all(c in "0123456789abcdef" for c in sig)


def test_sign_is_deterministic_for_same_actor_and_key():
    assert sign("alice", key=_KEY) == sign("alice", key=_KEY)


def test_sign_differs_for_different_actors():
    assert sign("alice", key=_KEY) != sign("bob", key=_KEY)


def test_sign_differs_for_different_keys():
    assert sign("alice", key=_KEY) != sign("alice", key="other-key")


def test_verify_round_trip_succeeds():
    sig = sign("alice@clinic", key=_KEY)
    assert verify("alice@clinic", sig, key=_KEY) is True


def test_verify_fails_on_tampered_actor():
    sig = sign("alice", key=_KEY)
    assert verify("bob", sig, key=_KEY) is False


def test_verify_fails_on_tampered_signature():
    sig = sign("alice", key=_KEY)
    tampered = ("0" if sig[0] != "0" else "1") + sig[1:]
    assert verify("alice", tampered, key=_KEY) is False


def test_verify_fails_on_wrong_key():
    sig = sign("alice", key=_KEY)
    assert verify("alice", sig, key="wrong-key") is False


def test_verify_returns_false_on_empty_inputs():
    assert verify("", "abc", key=_KEY) is False
    assert verify("alice", "", key=_KEY) is False
    assert verify("alice", "abc", key="") is False


def test_sign_raises_on_empty_key():
    import pytest

    with pytest.raises(ValueError):
        sign("alice", key="")

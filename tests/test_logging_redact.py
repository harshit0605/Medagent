"""Unit tests for the PII redaction helper used in log lines."""

from __future__ import annotations

from app.logging_redact import redact_phone


def test_redacts_e164_phone_keeps_last_four():
    out = redact_phone("+919876543210")
    # 13-char input → 9 asterisks + last 4 digits
    assert out == "*********3210"
    assert "987654" not in out


def test_redacts_test_shaped_phone():
    # The integration suite seeds phones shaped ``onb-asthma-aabbccdd``
    # (see tests/conftest.py phone_re). They aren't real PII but they
    # still leak inferred ordering / suite metadata into logs.
    out = redact_phone("onb-asthma-abcd1234")
    assert out.endswith("1234")
    assert "asthma" not in out
    assert out.count("*") == len("onb-asthma-abcd1234") - 4


def test_short_input_collapses_to_sentinel():
    # Below the 6-char minimum we don't leak even a tail.
    assert redact_phone("abc") == "<redacted>"
    assert redact_phone("12345") == "<redacted>"


def test_none_input_returns_sentinel():
    assert redact_phone(None) == "<redacted>"
    assert redact_phone("") == "<redacted>"


def test_whitespace_is_stripped_before_measuring():
    # ``+91 98765 43210`` would otherwise count whitespace toward length.
    assert redact_phone("+91 98765 43210") == "*********3210"


def test_non_str_coerces_via_str():
    # Defensive: log call sites occasionally pass numeric ids.
    # 12-digit int → 8 asterisks + last 4 digits.
    assert redact_phone(919876543210) == "********3210"  # type: ignore[arg-type]

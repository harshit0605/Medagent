"""Unit tests for the language allowlist + helpers."""

from __future__ import annotations

from app import i18n


def test_supported_codes_contain_known_languages():
    expected = {"en", "hi", "ta", "te", "bn", "mr", "gu", "kn", "ml", "pa"}
    assert expected <= i18n.SUPPORTED_LANGUAGE_CODES


def test_default_is_english():
    assert i18n.DEFAULT_LANGUAGE_CODE == "en"


def test_is_supported_rejects_garbage():
    assert i18n.is_supported("en")
    assert i18n.is_supported("hi")
    assert not i18n.is_supported("xx")
    assert not i18n.is_supported("")
    assert not i18n.is_supported(None)


def test_language_label_falls_back_for_unknown():
    """Unknown codes display uppercased so a misconfigured patient
    surfaces visibly in ops UI rather than silently looking like
    English."""
    assert i18n.language_label("hi") == "Hindi"
    assert i18n.language_label("xx") == "XX"
    assert i18n.language_label(None) == "English"


def test_llm_hint_includes_script_for_indian_languages():
    """The LLM prompt embeds the hint verbatim — having the script
    name in there pushes the model away from Roman transliteration."""
    assert "Devanagari" in i18n.llm_hint("hi")
    assert "Tamil" in i18n.llm_hint("ta")
    assert "Bangla" in i18n.llm_hint("bn")
    # English short-circuits to plain "English" so the prompt stays
    # the original wording.
    assert i18n.llm_hint("en") == "English"
    assert i18n.llm_hint(None) == "English"
    # Unknown codes default to English so we never ship a malformed prompt.
    assert i18n.llm_hint("xx") == "English"

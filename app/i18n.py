"""Language allowlist + helpers shared across orchestrator + scheduler.

V1 supports a small, India-leaning set. Each entry maps the ISO code
to a human-readable label (for the ops UI dropdown) and an LLM
"language hint" string the prompt can reference verbatim. Adding a
new language is a constants-only change — keep entries close to the
languages real patients write to us in.

The validation predicate ``is_supported`` powers the orchestrator
endpoints' write-time check so we don't accept arbitrary codes that
the LLM can't reasonably honour.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LanguageOption:
    code: str
    label: str
    llm_hint: str


# Order matches the patient-detail dropdown — English first as the
# default, then the most common Indian languages by speaker count.
SUPPORTED_LANGUAGES: tuple[LanguageOption, ...] = (
    LanguageOption("en", "English", "English"),
    LanguageOption("hi", "Hindi", "Hindi (Devanagari script)"),
    LanguageOption("ta", "Tamil", "Tamil (Tamil script)"),
    LanguageOption("te", "Telugu", "Telugu (Telugu script)"),
    LanguageOption("bn", "Bengali", "Bengali (Bangla script)"),
    LanguageOption("mr", "Marathi", "Marathi (Devanagari script)"),
    LanguageOption("gu", "Gujarati", "Gujarati (Gujarati script)"),
    LanguageOption("kn", "Kannada", "Kannada (Kannada script)"),
    LanguageOption("ml", "Malayalam", "Malayalam (Malayalam script)"),
    LanguageOption("pa", "Punjabi", "Punjabi (Gurmukhi script)"),
)
SUPPORTED_LANGUAGE_CODES: frozenset[str] = frozenset(
    o.code for o in SUPPORTED_LANGUAGES
)
DEFAULT_LANGUAGE_CODE: str = "en"


def is_supported(code: str | None) -> bool:
    if not code:
        return False
    return code in SUPPORTED_LANGUAGE_CODES


def language_label(code: str | None) -> str:
    """Human-readable label for the ops UI. Falls back to the raw
    code (uppercased) when unknown — visible signal that an unsupported
    value snuck in."""
    if not code:
        return "English"
    for opt in SUPPORTED_LANGUAGES:
        if opt.code == code:
            return opt.label
    return code.upper()


def llm_hint(code: str | None) -> str:
    """The string the LLM prompt embeds — e.g. ``Hindi (Devanagari
    script)``. Designed to be specific enough that the model gets
    both the language AND the script right; ``Hindi`` alone has been
    known to slip into Roman script in some prompts."""
    if not code or code == DEFAULT_LANGUAGE_CODE:
        return "English"
    for opt in SUPPORTED_LANGUAGES:
        if opt.code == code:
            return opt.llm_hint
    return "English"

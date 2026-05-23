"""Unit tests for the voice-note transcription adapter (no whisper, no DB).

The marker parse + ``maybe_transcribe`` orchestration are the seams the rest
of the app depends on, so they're the focus here. faster-whisper itself is
import-guarded; we mock the model/resolve/transcribe internals so the suite
runs green WITHOUT the optional ``[voice]`` extra installed.
"""

from __future__ import annotations

from pathlib import Path

from services.orchestrator import transcription as t


# ---- marker detection ------------------------------------------------------


def test_looks_like_voice_note_matches_marker():
    assert t.looks_like_voice_note(
        "[voice-note] public_path=/uploads/voice/x.ogg mime=audio/ogg"
    )
    # mime is optional.
    assert t.looks_like_voice_note("[voice-note] public_path=/u/x.ogg")
    assert not t.looks_like_voice_note("sugar 140")
    assert not t.looks_like_voice_note("[prescription-upload] public_path=/u/x.jpg")
    assert not t.looks_like_voice_note("")
    assert not t.looks_like_voice_note(None)


def test_parse_voice_marker_extracts_path_and_mime():
    parsed = t.parse_voice_marker(
        "[voice-note] public_path=/uploads/voice/x.ogg mime=audio/ogg"
    )
    assert parsed == ("/uploads/voice/x.ogg", "audio/ogg")
    # mime optional → None.
    assert t.parse_voice_marker("[voice-note] public_path=/u/y.ogg") == (
        "/u/y.ogg",
        None,
    )
    # non-marker → None.
    assert t.parse_voice_marker("hello there") is None
    assert t.parse_voice_marker("") is None


# ---- maybe_transcribe orchestration ----------------------------------------


def test_maybe_transcribe_returns_none_for_non_marker():
    # Not a voice marker → caller leaves the text untouched.
    assert t.maybe_transcribe("just a normal message") is None
    assert t.maybe_transcribe("") is None
    assert t.maybe_transcribe(None) is None


def test_maybe_transcribe_returns_transcript_on_success(monkeypatch):
    monkeypatch.setattr(t, "_resolve_audio_to_local", lambda _p: Path("/tmp/fake.ogg"))
    monkeypatch.setattr(t, "transcribe_audio", lambda _p: "sugar one forty")
    out = t.maybe_transcribe(
        "[voice-note] public_path=/uploads/voice/x.ogg mime=audio/ogg"
    )
    assert out == "sugar one forty"


def test_maybe_transcribe_falls_back_when_audio_unresolvable(monkeypatch):
    # Audio can't be fetched (no local file, no PUBLIC_MEDIA_BASE_URL) → the
    # patient still gets a polite "please type" rather than silence.
    monkeypatch.setattr(t, "_resolve_audio_to_local", lambda _p: None)
    out = t.maybe_transcribe("[voice-note] public_path=/u/x.ogg")
    assert out == t._FALLBACK_TEXT


def test_maybe_transcribe_falls_back_when_transcription_empty(monkeypatch):
    # Audio resolved but whisper returned nothing (decode error / disabled).
    monkeypatch.setattr(t, "_resolve_audio_to_local", lambda _p: Path("/tmp/fake.ogg"))
    monkeypatch.setattr(t, "transcribe_audio", lambda _p: None)
    out = t.maybe_transcribe("[voice-note] public_path=/u/x.ogg")
    assert out == t._FALLBACK_TEXT


def test_transcribe_audio_returns_none_when_model_unavailable(monkeypatch):
    # Simulate the [voice] extra not being installed — degrade, don't raise.
    monkeypatch.setattr(t, "_get_model", lambda: None)
    assert t.transcribe_audio("/tmp/whatever.ogg") is None

"""Voice-note transcription adapter.

WhatsApp voice notes are a first-class input in the SoT ("text + voice
notes", voice glucose capture, voice trigger diary). The ingress layer
(Next.js webhook / gateway) downloads the audio media and forwards it as
a marker, mirroring the prescription-image path:

    [voice-note] public_path=/uploads/voice/abc.ogg mime=audio/ogg

This module turns that marker into text BEFORE routing, so a spoken
"sugar 140" reaches the vitals handler, a spoken "I feel dizzy" reaches
triage, etc. — voice transparently reuses every existing text flow.

Transcription is local (faster-whisper, CPU/int8) so no audio leaves the
host. The dependency is an optional ``[voice]`` extra; when it isn't
installed (or transcription fails), we degrade to a polite fallback that
asks the patient to type — never silently drop the message.

Seams kept deliberately small + mockable:
- ``looks_like_voice_note`` / ``parse_voice_marker`` — pure regex
- ``transcribe_audio(path)`` — the one place faster-whisper is touched
- ``maybe_transcribe(text)`` — orchestration; returns transcript or
  fallback text, or ``None`` when the input isn't a voice marker
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


_VOICE_RE = re.compile(
    r"^\s*\[voice-note\]\s+"
    r"public_path=(?P<path>\S+)"
    r"(?:\s+mime=(?P<mime>\S+))?"
    r"\s*$"
)

# Spoken-message fallback when transcription is unavailable / fails. Routes
# to the normal LLM/compose path (it's plain text), and the copy invites a
# retype so the patient isn't stuck.
_FALLBACK_TEXT = (
    "(voice note received — transcription is unavailable right now; "
    "please type your message)"
)

# Whisper model size. ``base`` is the speed/accuracy sweet spot for short
# clinical utterances on CPU; override via env for accuracy-sensitive
# deployments.
_WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")

# Cached model instance — loading is expensive (weights + ctranslate2
# init), so we keep one per process.
_model = None


def looks_like_voice_note(text: str | None) -> bool:
    return bool(text and _VOICE_RE.match(text))


def parse_voice_marker(text: str) -> tuple[str, str | None] | None:
    """Return ``(public_path, mime)`` from a voice marker, or ``None``."""
    m = _VOICE_RE.match(text or "")
    if m is None:
        return None
    return m.group("path"), m.group("mime")


def _resolve_audio_to_local(public_path: str) -> Path | None:
    """Resolve the marker's ``public_path`` to a readable local file.

    Two cases, in order:
      1. It's already a local filesystem path that exists (shared upload
         volume between the ingress + orchestrator) — use it directly.
      2. Otherwise treat it as a path under ``PUBLIC_MEDIA_BASE_URL`` and
         download to a temp file.
    Returns ``None`` when the audio can't be obtained.
    """
    p = Path(public_path)
    if p.is_file():
        return p

    base = os.getenv("PUBLIC_MEDIA_BASE_URL", "").rstrip("/")
    if not base:
        log.warning(
            "voice note %s not local and PUBLIC_MEDIA_BASE_URL unset",
            public_path,
        )
        return None
    url = f"{base}/{public_path.lstrip('/')}"
    try:
        import httpx

        with httpx.Client(timeout=20.0) as client:
            resp = client.get(url)
            resp.raise_for_status()
        suffix = Path(public_path).suffix or ".ogg"
        tmp = tempfile.NamedTemporaryFile(
            delete=False, suffix=suffix
        )
        tmp.write(resp.content)
        tmp.flush()
        tmp.close()
        return Path(tmp.name)
    except Exception as exc:  # noqa: BLE001 — degrade to fallback
        log.warning("voice note download failed (%s): %s", url, exc)
        return None


def _get_model():
    """Lazy-load the faster-whisper model. Returns ``None`` when the
    optional dependency isn't installed (feature degrades gracefully)."""
    global _model
    if _model is not None:
        return _model
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        log.info(
            "faster-whisper not installed; voice transcription disabled "
            "(install the [voice] extra to enable)"
        )
        return None
    # int8 on CPU — small footprint, no GPU assumption.
    _model = WhisperModel(_WHISPER_MODEL, device="cpu", compute_type="int8")
    return _model


def transcribe_audio(local_path: Path | str) -> str | None:
    """Transcribe an audio file to text. Returns ``None`` on any failure
    (missing dep, decode error) so the caller can fall back."""
    model = _get_model()
    if model is None:
        return None
    try:
        segments, _info = model.transcribe(str(local_path), beam_size=1)
        text = " ".join(seg.text.strip() for seg in segments).strip()
        return text or None
    except Exception as exc:  # noqa: BLE001
        log.warning("whisper transcription failed: %s", exc)
        return None


def maybe_transcribe(text: str | None) -> str | None:
    """If ``text`` is a voice-note marker, resolve + transcribe the audio
    and return the transcript (or a typed-fallback message when
    transcription is unavailable). Returns ``None`` when the input isn't a
    voice marker, so the caller leaves the text untouched.
    """
    parsed = parse_voice_marker(text or "")
    if parsed is None:
        return None
    public_path, _mime = parsed

    local = _resolve_audio_to_local(public_path)
    if local is None:
        return _FALLBACK_TEXT

    transcript = transcribe_audio(local)
    if not transcript:
        return _FALLBACK_TEXT

    log.info("voice note transcribed (%d chars)", len(transcript))
    return transcript

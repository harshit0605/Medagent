"""Unit tests for the side-effect history extraction helper.

The helper parses ticket notes written by ``side_effect_handler``
into a clean ``reported_text`` field for the patient-detail DTO.
The notes format is owned by us (we write it, we read it), so a
parser is a reasonable v1 — much simpler than a migration to add
a structured ``inbound_text`` column. If the format ever changes,
this is the only place to update.

Failure mode behaviour matters here:

    - Missing block → return None (legacy / manually-created
      tickets don't have the block; UI falls back to raw notes)
    - Malformed block → return None rather than crashing
    - Trailing context (regimen list) → don't bleed into the
      extracted text
"""

from __future__ import annotations

from services.orchestrator.main import _extract_reported_text


# ---- Happy path ---------------------------------------------------------


def test_extracts_single_line_quote():
    notes = (
        "[side-effect report]\n"
        "Reported at: 2026-05-07T22:00:00+00:00\n"
        "\n"
        "Patient said:\n"
        "  > metformin gave me severe headaches\n"
        "\n"
        "Active regimens at time of report:\n"
        "  - Metformin 500 mg"
    )
    out = _extract_reported_text(notes)
    assert out == "metformin gave me severe headaches"


def test_extracts_multi_line_quote():
    """A patient might send a multi-line message that the handler
    wrote across multiple ``  > `` lines. The extractor must
    preserve the line breaks so the doctor sees the full story."""
    notes = (
        "[side-effect report]\n"
        "Reported at: 2026-05-07T22:00:00+00:00\n"
        "\n"
        "Patient said:\n"
        "  > started feeling dizzy this morning\n"
        "  > also a bit nauseous, can barely walk\n"
        "  > been taking the new prescription for 2 days\n"
        "\n"
        "Active regimens at time of report:\n"
        "  - Atorvastatin 10 mg"
    )
    out = _extract_reported_text(notes)
    assert out == (
        "started feeling dizzy this morning\n"
        "also a bit nauseous, can barely walk\n"
        "been taking the new prescription for 2 days"
    )


def test_handles_no_trailing_regimen_block():
    """Tickets without an Active-regimens block (e.g. patient on
    no active medication) still extract the reported text cleanly
    — the block-end marker is the blank line, not the regimens
    section."""
    notes = (
        "[side-effect report]\n"
        "Reported at: 2026-05-07T22:00:00+00:00\n"
        "\n"
        "Patient said:\n"
        "  > rash everywhere"
    )
    out = _extract_reported_text(notes)
    assert out == "rash everywhere"


# ---- Negative paths -----------------------------------------------------


def test_missing_marker_returns_none():
    """Legacy / manually-created tickets without the standard
    ``Patient said:`` marker → return None. UI falls back to
    rendering raw notes rather than crashing."""
    assert _extract_reported_text("just a free-form note") is None


def test_marker_present_but_no_quote_lines_returns_none():
    """Marker followed immediately by a non-quote line means the
    block was malformed at write-time. Don't pretend we extracted
    anything — return None so the UI shows its fallback."""
    notes = "Patient said:\nsome non-quoted text"
    assert _extract_reported_text(notes) is None


def test_empty_notes_returns_none():
    assert _extract_reported_text(None) is None
    assert _extract_reported_text("") is None
    assert _extract_reported_text("   \n\n  ") is None


def test_quote_marker_only_returns_none():
    """Marker present, but the only "quote" line is empty → None."""
    notes = "Patient said:\n  > "
    assert _extract_reported_text(notes) is None


def test_strips_quote_marker_and_whitespace():
    """Various ``>``-quote indentations should all collapse to the
    bare text. Indentation is a write-time concern, not a
    read-time one."""
    notes = "Patient said:\n>>>   nausea\n  > vomiting"
    out = _extract_reported_text(notes)
    assert out == "nausea\nvomiting"

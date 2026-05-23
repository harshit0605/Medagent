"""Unit tests for the asthma self-report parsers (no DB).

The rescue-use + trigger parsers are the safety-critical, high-volume bits:
they must capture real reports and reject controller doses / stray numbers.
"""

from __future__ import annotations

from services.orchestrator.asthma_handler import (
    looks_like_asthma_log,
    parse_rescue_use,
    parse_trigger,
)


# ---- rescue-inhaler usage --------------------------------------------------


def test_parse_rescue_use_explicit_counts():
    assert parse_rescue_use("used my reliever 3 times") == 3
    assert parse_rescue_use("took 2 puffs of salbutamol") == 2
    assert parse_rescue_use("4 puffs ventolin") == 4
    assert parse_rescue_use("used inhaler 3 times today") == 3
    assert parse_rescue_use("my reliever 5x") == 5


def test_parse_rescue_use_word_counts():
    assert parse_rescue_use("rescue inhaler twice today") == 2
    assert parse_rescue_use("used my reliever once") == 1
    assert parse_rescue_use("needed albuterol three times") == 3


def test_parse_rescue_use_default_single_use():
    # Strong reliever keyword + usage verb, no number → a single use.
    assert parse_rescue_use("used my rescue inhaler") == 1
    assert parse_rescue_use("had to use my reliever") == 1


def test_parse_rescue_use_rejects_controller_and_noise():
    # Bare "inhaler" with no count/usage → likely a controller dose, not rescue.
    assert parse_rescue_use("took my inhaler this morning") is None
    # A statement of possession, not a use.
    assert parse_rescue_use("I have a rescue inhaler") is None
    # Non-asthma numbers.
    assert parse_rescue_use("sugar 140") is None
    assert parse_rescue_use("peak flow 400") is None
    assert parse_rescue_use("took 2 tablets") is None
    assert parse_rescue_use("") is None
    assert parse_rescue_use(None) is None


def test_parse_rescue_use_rejects_implausible_count():
    assert parse_rescue_use("used my reliever 99 times") is None


# ---- trigger diary ---------------------------------------------------------


def test_parse_trigger_variants():
    assert parse_trigger("trigger: dust") == "dust"
    assert parse_trigger("triggered by exercise") == "exercise"
    assert parse_trigger("my trigger is cold air") == "cold air"
    assert parse_trigger("set off by smoke") == "smoke"
    # Trailing filler trimmed.
    assert parse_trigger("triggered by pollen today") == "pollen"


def test_parse_trigger_negatives():
    assert parse_trigger("feeling fine thanks") is None
    assert parse_trigger("sugar 140") is None
    assert parse_trigger("") is None
    assert parse_trigger(None) is None


# ---- combined router gate --------------------------------------------------


def test_looks_like_asthma_log():
    assert looks_like_asthma_log("used my reliever 3 times") is True
    assert looks_like_asthma_log("trigger: dust") is True
    assert looks_like_asthma_log("took my inhaler this morning") is False
    assert looks_like_asthma_log("just saying hi") is False

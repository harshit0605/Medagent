"""Unit tests for the onboarding handler — state machine + parsers
+ the hardening gates (name validation, garbage-cohorts rejection,
multi-language render, retry escalation, stale reset).

DB session is mocked so we cover only the routing + parsing logic.
"""

from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone

import pytest

from services.orchestrator import onboarding_handler as h


# ---- Cohort parsing -------------------------------------------------------


def test_parse_cohorts_keywords():
    assert h.parse_cohorts("diabetes and fall risk") == {
        "diabetes": True,
        "cardiac": False,
        "fall_risk": True,
    }


def test_parse_cohorts_numeric_picks():
    assert h.parse_cohorts("1, 2") == {
        "diabetes": True,
        "cardiac": True,
        "fall_risk": False,
    }


def test_parse_cohorts_alternative_terms():
    assert h.parse_cohorts("heart condition")["cardiac"] is True
    assert h.parse_cohorts("I'm diabetic")["diabetes"] is True
    assert h.parse_cohorts("balance issues")["fall_risk"] is True


def test_parse_cohorts_explicit_none_returns_all_false():
    assert h.parse_cohorts("none of these") == {
        "diabetes": False,
        "cardiac": False,
        "fall_risk": False,
    }
    assert h.parse_cohorts("4") == {
        "diabetes": False,
        "cardiac": False,
        "fall_risk": False,
    }


def test_parse_cohorts_unparseable_returns_none():
    """Garbage input must NOT silently commit all-False — that previous
    behaviour lost cohort flags for patients who typed unusual replies.
    Caller is expected to re-prompt when ``parse_cohorts`` returns None."""
    assert h.parse_cohorts("xyz random text") is None
    assert h.parse_cohorts("") is None
    assert h.parse_cohorts("   ") is None


# ---- Name validation -------------------------------------------------------


def test_validate_name_accepts_normal_names():
    assert h.validate_name("Asha Mehta") == "Asha Mehta"
    assert h.validate_name("  Asha  ") == "Asha"
    # Indic script — Hindi / Tamil patient typing native name.
    assert h.validate_name("आशा मेहता") == "आशा मेहता"
    assert h.validate_name("ஆஷா") == "ஆஷா"


def test_validate_name_caps_at_100_chars():
    assert h.validate_name("A" * 200) == "A" * 100


def test_validate_name_rejects_empty_and_short():
    assert h.validate_name(None) is None
    assert h.validate_name("") is None
    assert h.validate_name("   ") is None
    assert h.validate_name("x") is None  # single letter — re-prompt


def test_validate_name_rejects_all_digits_or_symbols():
    """All-numeric input is almost certainly a phone number / typo, not
    a name. All-symbols likewise."""
    assert h.validate_name("9876543210") is None
    assert h.validate_name("@@@") is None
    assert h.validate_name("...") is None


def test_validate_name_rejects_action_tap_markers():
    """Defensive — the upstream router filters these to their own
    handlers, but a stale ``needs_name`` patient who somehow sees
    one of these inputs must NOT have the marker stamped as their
    full_name. That would corrupt their profile permanently."""
    for marker in (
        "[dose-action] taken adherence_event_id=4",
        "[refill-action] confirm",
        "[lab-action] reschedule",
        "[recap-action] question",
        "[caregiver-action] confirm caregiver_id=1",
        "[prescription-upload]",
    ):
        assert h.validate_name(marker) is None, marker


# ---- Consent parsing ------------------------------------------------------


@pytest.mark.parametrize(
    "text", ["YES", "yes", "y", "yeah", "ok please", "sure"]
)
def test_parse_consent_yes_variants(text):
    assert h.parse_consent(text) is True


@pytest.mark.parametrize("text", ["NO", "no", "n", "nope", "skip"])
def test_parse_consent_no_variants(text):
    assert h.parse_consent(text) is False


def test_parse_consent_unparseable_returns_none():
    assert h.parse_consent("maybe later") is None
    assert h.parse_consent("") is None


# ---- is_onboarding_active -------------------------------------------------


def test_is_onboarding_active_states():
    assert h.is_onboarding_active("pending")
    assert h.is_onboarding_active("needs_name")
    assert h.is_onboarding_active("needs_cohorts")
    assert h.is_onboarding_active("needs_consent")
    assert not h.is_onboarding_active("done")
    assert not h.is_onboarding_active(None)


# ---- Localised copy -------------------------------------------------------


def test_render_english_default():
    assert "I'm your care assistant" in h._render("greeting", "en")


def test_render_hindi_translates():
    out = h._render("greeting", "hi")
    # Devanagari नमस्ते — confirms we picked up the Hindi entry.
    assert "नमस्ते" in out


def test_render_unknown_language_falls_back_to_english():
    """Languages without a translations entry use the English copy.
    The dropdown allowlist constrains writes, but legacy rows or new
    codes added before translations land must still render readable
    text rather than crashing."""
    en = h._render("greeting", "en")
    assert h._render("greeting", "ta") == en  # Tamil — no entry yet
    assert h._render("greeting", "xx") == en  # unknown
    assert h._render("greeting", None) == en


def test_render_substitutes_kwargs():
    out = h._render("cohorts_prompt", "en", name="Asha")
    assert "Thanks Asha!" in out
    out_hi = h._render("cohorts_prompt", "hi", name="आशा")
    assert "आशा" in out_hi


# ---- _is_stale ------------------------------------------------------------


def test_is_stale_null_step_at_returns_false():
    """Legacy rows pre-migration have NULL ``onboarding_step_at`` —
    we must NOT treat that as stale (would reset every legacy row on
    next inbound)."""
    p = types.SimpleNamespace(onboarding_step_at=None)
    assert h._is_stale(p) is False


def test_is_stale_recent_returns_false():
    p = types.SimpleNamespace(
        onboarding_step_at=datetime.now(timezone.utc)
    )
    assert h._is_stale(p) is False


def test_is_stale_old_returns_true():
    old = datetime.now(timezone.utc) - timedelta(
        days=h.STALE_AFTER_DAYS + 1
    )
    p = types.SimpleNamespace(onboarding_step_at=old)
    assert h._is_stale(p) is True


def test_is_stale_naive_datetime_treated_as_utc():
    """Postgres rows occasionally come back naive; the helper must not
    crash and should treat them as UTC rather than mis-classifying."""
    naive_old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(
        days=h.STALE_AFTER_DAYS + 1
    )
    p = types.SimpleNamespace(onboarding_step_at=naive_old)
    assert h._is_stale(p) is True


# ---- handle_onboarding state transitions ----------------------------------


class _NoopAsyncSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None

    async def commit(self):
        return None

    async def get(self, *_a, **_k):
        return None


def _patient(
    *,
    id=1,
    phone="9100",
    step,
    preferred_language="en",
    onboarding_retry_count=0,
    onboarding_step_at=None,
):
    return types.SimpleNamespace(
        id=id,
        phone=phone,
        full_name="Patient 9100",
        onboarding_step=step,
        cohort_diabetes=False,
        cohort_cardiac=False,
        cohort_fall_risk=False,
        consent_sms=False,
        preferred_language=preferred_language,
        onboarding_retry_count=onboarding_retry_count,
        onboarding_step_at=onboarding_step_at,
    )


def _patch(monkeypatch, *, patient, open_ticket=None):
    """Mock the DB session + the patients_repo + ops_tickets_repo
    surfaces the handler now touches.

    Returns a captured-state dict the test can introspect:
        updates       — list of update_onboarding (id, kwargs)
        retry_bumps   — list of patient_ids passed to bump_onboarding_retry
        ticket_finds  — list of (patient_id, category) lookups
        tickets       — list of created tickets (each is a SimpleNamespace)
    """
    captured = {
        "updates": [],
        "retry_bumps": [],
        "ticket_finds": [],
        "tickets": [],
    }
    # Container so the closure can mutate the "currently open ticket"
    # as new ones are created.
    state = {"open_ticket": open_ticket}

    def factory():
        return _NoopAsyncSession()

    monkeypatch.setattr(h, "get_sessionmaker", lambda: factory)

    async def get_by_phone(_db, _phone):
        return patient

    async def update_onboarding(_db, _id, **kwargs):
        captured["updates"].append((_id, kwargs))
        # Mutate the source so subsequent calls see new state. The real
        # repo also resets retry to 0 + stamps step_at when step is
        # passed; mirror that here so tests catch retry-reset bugs.
        for k, v in kwargs.items():
            setattr(patient, k, v)
        if "step" in kwargs:
            patient.onboarding_retry_count = 0
            patient.onboarding_step_at = datetime.now(timezone.utc)
        return patient

    async def bump_onboarding_retry(_db, _id):
        captured["retry_bumps"].append(_id)
        patient.onboarding_retry_count = (
            patient.onboarding_retry_count or 0
        ) + 1
        return patient.onboarding_retry_count

    async def find_open(_db, *, patient_id, category):
        captured["ticket_finds"].append((patient_id, category))
        return state["open_ticket"]

    async def create_ticket(_db, *, patient_id, category, priority, sla_minutes, notes=None):
        ticket = types.SimpleNamespace(
            id=len(captured["tickets"]) + 100,
            patient_id=patient_id,
            category=category,
            priority=priority,
            sla_minutes=sla_minutes,
            notes=notes,
        )
        captured["tickets"].append(ticket)
        # New tickets become "the open ticket" so subsequent finds in
        # the same handler call see it (idempotency check).
        state["open_ticket"] = ticket
        return ticket

    monkeypatch.setattr(h.patients_repo, "get_by_phone", get_by_phone)
    monkeypatch.setattr(
        h.patients_repo, "update_onboarding", update_onboarding
    )
    monkeypatch.setattr(
        h.patients_repo, "bump_onboarding_retry", bump_onboarding_retry
    )
    monkeypatch.setattr(
        h.ops_tickets_repo, "find_open_for_patient_category", find_open
    )
    monkeypatch.setattr(h.ops_tickets_repo, "create", create_ticket)
    return captured


async def test_handle_pending_sends_greeting_advances_to_needs_name(monkeypatch):
    p = _patient(step=h.PENDING)
    captured = _patch(monkeypatch, patient=p)
    delta = await h.handle_onboarding(patient_phone="9100", new_user_text="hi")
    assert delta is not None
    assert "your full name" in delta["response_body"].lower()
    assert delta["audit_reasons"] == ["onboarding_greeting"]
    assert captured["updates"][0][1]["step"] == h.NEEDS_NAME


async def test_handle_pending_greets_in_hindi_when_preferred(monkeypatch):
    """A patient whose preferred_language was auto-detected to ``hi``
    on a prior turn should see the Hindi greeting, not English."""
    p = _patient(step=h.PENDING, preferred_language="hi")
    _patch(monkeypatch, patient=p)
    delta = await h.handle_onboarding(patient_phone="9100", new_user_text="hi")
    assert delta is not None
    assert "नमस्ते" in delta["response_body"]


async def test_handle_needs_name_stores_full_name_advances_to_needs_cohorts(monkeypatch):
    p = _patient(step=h.NEEDS_NAME)
    captured = _patch(monkeypatch, patient=p)
    delta = await h.handle_onboarding(
        patient_phone="9100", new_user_text="Asha Mehta"
    )
    assert delta is not None
    assert "Asha Mehta" in delta["response_body"]
    assert "diabetes" in delta["response_body"].lower()
    assert captured["updates"][0][1]["step"] == h.NEEDS_COHORTS
    assert captured["updates"][0][1]["full_name"] == "Asha Mehta"


async def test_handle_needs_name_empty_input_re_asks(monkeypatch):
    p = _patient(step=h.NEEDS_NAME)
    captured = _patch(monkeypatch, patient=p)
    delta = await h.handle_onboarding(patient_phone="9100", new_user_text="   ")
    assert delta is not None
    assert "didn't catch" in delta["response_body"].lower()
    assert delta["audit_reasons"] == ["onboarding_name_invalid"]
    # No state change.
    assert captured["updates"] == []
    # First failure — retry bumped to 1, no ticket yet.
    assert captured["retry_bumps"] == [p.id]
    assert captured["tickets"] == []


async def test_handle_needs_name_action_marker_re_asks(monkeypatch):
    """Defensive: a stale ``needs_name`` patient who taps a button
    elsewhere shouldn't get the marker stamped as their full_name."""
    p = _patient(step=h.NEEDS_NAME)
    captured = _patch(monkeypatch, patient=p)
    delta = await h.handle_onboarding(
        patient_phone="9100",
        new_user_text="[dose-action] taken adherence_event_id=4",
    )
    assert delta["audit_reasons"] == ["onboarding_name_invalid"]
    assert captured["updates"] == []


async def test_handle_needs_name_all_digits_re_asks(monkeypatch):
    """All-numeric input is almost certainly a phone-number typo, not
    a name. Re-prompt rather than store ``9876543210`` as full_name."""
    p = _patient(step=h.NEEDS_NAME)
    captured = _patch(monkeypatch, patient=p)
    delta = await h.handle_onboarding(
        patient_phone="9100", new_user_text="9876543210"
    )
    assert delta["audit_reasons"] == ["onboarding_name_invalid"]
    assert captured["updates"] == []


async def test_handle_needs_cohorts_sets_flags_advances_to_needs_consent(monkeypatch):
    p = _patient(step=h.NEEDS_COHORTS)
    captured = _patch(monkeypatch, patient=p)
    delta = await h.handle_onboarding(
        patient_phone="9100", new_user_text="diabetes, fall risk"
    )
    assert delta is not None
    assert "send you reminders" in delta["response_body"].lower()
    assert captured["updates"][0][1]["step"] == h.NEEDS_CONSENT
    assert captured["updates"][0][1]["cohort_diabetes"] is True
    assert captured["updates"][0][1]["cohort_fall_risk"] is True
    assert captured["updates"][0][1]["cohort_cardiac"] is False


async def test_handle_needs_cohorts_none_advances_with_all_false(monkeypatch):
    p = _patient(step=h.NEEDS_COHORTS)
    captured = _patch(monkeypatch, patient=p)
    delta = await h.handle_onboarding(
        patient_phone="9100", new_user_text="none"
    )
    assert delta is not None
    args = captured["updates"][0][1]
    assert args["step"] == h.NEEDS_CONSENT
    assert args["cohort_diabetes"] is False
    assert args["cohort_cardiac"] is False
    assert args["cohort_fall_risk"] is False


async def test_handle_needs_cohorts_garbage_re_asks(monkeypatch):
    """Garbage input at cohorts step must re-prompt — the previous
    silent-commit-as-none behaviour was a data-loss bug."""
    p = _patient(step=h.NEEDS_COHORTS)
    captured = _patch(monkeypatch, patient=p)
    delta = await h.handle_onboarding(
        patient_phone="9100", new_user_text="qwertyuiop"
    )
    assert delta is not None
    assert delta["audit_reasons"] == ["onboarding_cohorts_unclear"]
    # State unchanged — patient gets another shot.
    assert captured["updates"] == []


async def test_handle_needs_consent_yes_sets_consent_advances_to_done(monkeypatch):
    p = _patient(step=h.NEEDS_CONSENT)
    captured = _patch(monkeypatch, patient=p)
    delta = await h.handle_onboarding(patient_phone="9100", new_user_text="yes")
    assert delta is not None
    assert "all set" in delta["response_body"].lower()
    assert captured["updates"][0][1]["step"] == h.DONE
    assert captured["updates"][0][1]["consent_sms"] is True


async def test_handle_needs_consent_no_sets_consent_false_advances_to_done(monkeypatch):
    p = _patient(step=h.NEEDS_CONSENT)
    captured = _patch(monkeypatch, patient=p)
    delta = await h.handle_onboarding(patient_phone="9100", new_user_text="no")
    assert delta is not None
    assert "no reminders" in delta["response_body"].lower()
    assert captured["updates"][0][1]["step"] == h.DONE
    assert captured["updates"][0][1]["consent_sms"] is False


async def test_handle_needs_consent_unclear_re_asks(monkeypatch):
    p = _patient(step=h.NEEDS_CONSENT)
    captured = _patch(monkeypatch, patient=p)
    delta = await h.handle_onboarding(
        patient_phone="9100", new_user_text="hmm not sure"
    )
    assert delta is not None
    assert "yes" in delta["response_body"].lower()
    assert delta["audit_reasons"] == ["onboarding_consent_unclear"]
    # State unchanged.
    assert captured["updates"] == []


async def test_handle_needs_consent_yes_renders_in_hindi(monkeypatch):
    """Final ``done`` reply should respect preferred_language too —
    a Hindi patient who consents must see the Hindi success message."""
    p = _patient(step=h.NEEDS_CONSENT, preferred_language="hi")
    _patch(monkeypatch, patient=p)
    delta = await h.handle_onboarding(patient_phone="9100", new_user_text="yes")
    # ``सब तैयार`` is the leading line of the Hindi complete_yes copy.
    assert "सब तैयार" in delta["response_body"]


async def test_handle_done_returns_none_so_caller_falls_through(monkeypatch):
    p = _patient(step=h.DONE)
    _patch(monkeypatch, patient=p)
    out = await h.handle_onboarding(patient_phone="9100", new_user_text="hi")
    assert out is None


async def test_handle_unknown_step_returns_none(monkeypatch):
    p = _patient(step="some_unknown_step")
    _patch(monkeypatch, patient=p)
    out = await h.handle_onboarding(patient_phone="9100", new_user_text="hi")
    assert out is None


# ---- Retry escalation -----------------------------------------------------


async def test_retry_below_threshold_uses_standard_re_prompt(monkeypatch):
    """First and second invalid inputs at any step send the normal
    re-prompt copy and DO NOT open an ops ticket."""
    p = _patient(step=h.NEEDS_NAME, onboarding_retry_count=1)
    captured = _patch(monkeypatch, patient=p)
    delta = await h.handle_onboarding(patient_phone="9100", new_user_text="x")
    # 2nd consecutive invalid → retry now 2, still below threshold (3).
    assert p.onboarding_retry_count == 2
    assert delta["audit_reasons"] == ["onboarding_name_invalid"]
    assert "didn't catch" in delta["response_body"].lower()
    assert captured["tickets"] == []
    assert captured["ticket_finds"] == []  # not even probed


async def test_retry_at_threshold_creates_ticket_and_switches_copy(monkeypatch):
    """3rd invalid input crosses ESCALATION_THRESHOLD — the handler
    opens an ``onboarding_stuck`` ticket and switches the reply to the
    escalation copy."""
    p = _patient(step=h.NEEDS_NAME, onboarding_retry_count=2)
    captured = _patch(monkeypatch, patient=p)
    delta = await h.handle_onboarding(
        patient_phone="9100", new_user_text="@@@"
    )
    assert p.onboarding_retry_count == 3  # crossed threshold
    assert "teammate" in delta["response_body"].lower()
    assert delta["audit_reasons"] == [
        "onboarding_name_invalid",
        "onboarding_escalated",
    ]
    assert len(captured["tickets"]) == 1
    ticket = captured["tickets"][0]
    assert ticket.patient_id == p.phone
    assert ticket.category == h.ESCALATION_CATEGORY
    assert ticket.priority == h.ESCALATION_PRIORITY


async def test_retry_above_threshold_does_not_create_duplicate_ticket(monkeypatch):
    """4th and subsequent invalid inputs keep returning the escalation
    copy but must NOT open additional tickets — the existing one stays
    open until ops resolves it."""
    existing = types.SimpleNamespace(
        id=999,
        patient_id="9100",
        category=h.ESCALATION_CATEGORY,
    )
    p = _patient(step=h.NEEDS_NAME, onboarding_retry_count=3)
    captured = _patch(monkeypatch, patient=p, open_ticket=existing)
    delta = await h.handle_onboarding(
        patient_phone="9100", new_user_text="@@@"
    )
    assert p.onboarding_retry_count == 4
    assert "teammate" in delta["response_body"].lower()
    assert delta["audit_reasons"] == [
        "onboarding_name_invalid",
        "onboarding_escalated",
    ]
    # Probed for an existing ticket but did NOT create a duplicate.
    assert captured["ticket_finds"] == [
        (p.phone, h.ESCALATION_CATEGORY)
    ]
    assert captured["tickets"] == []


async def test_valid_input_after_escalation_resets_retry_and_advances(monkeypatch):
    """Once a stuck patient finally types something valid, state must
    advance and retry must reset to 0 — the open ticket persists for
    ops to resolve manually."""
    p = _patient(step=h.NEEDS_NAME, onboarding_retry_count=4)
    captured = _patch(monkeypatch, patient=p)
    delta = await h.handle_onboarding(
        patient_phone="9100", new_user_text="Asha Mehta"
    )
    assert p.onboarding_retry_count == 0  # reset by update_onboarding
    assert captured["updates"][0][1]["step"] == h.NEEDS_COHORTS
    assert "Asha Mehta" in delta["response_body"]
    assert captured["tickets"] == []  # no new ticket on advance


async def test_retry_escalation_works_at_cohorts_step(monkeypatch):
    """Escalation must fire at any active step, not just needs_name."""
    p = _patient(step=h.NEEDS_COHORTS, onboarding_retry_count=2)
    captured = _patch(monkeypatch, patient=p)
    delta = await h.handle_onboarding(
        patient_phone="9100", new_user_text="qwertyuiop"
    )
    assert p.onboarding_retry_count == 3
    assert delta["audit_reasons"] == [
        "onboarding_cohorts_unclear",
        "onboarding_escalated",
    ]
    assert len(captured["tickets"]) == 1


async def test_retry_escalation_works_at_consent_step(monkeypatch):
    p = _patient(step=h.NEEDS_CONSENT, onboarding_retry_count=2)
    captured = _patch(monkeypatch, patient=p)
    delta = await h.handle_onboarding(
        patient_phone="9100", new_user_text="hmm not sure"
    )
    assert p.onboarding_retry_count == 3
    assert delta["audit_reasons"] == [
        "onboarding_consent_unclear",
        "onboarding_escalated",
    ]
    assert len(captured["tickets"]) == 1


# ---- Stale reset ----------------------------------------------------------


async def test_stale_patient_at_needs_name_resets_to_pending(monkeypatch):
    """A patient who ghosted the flow >30 days ago gets reset to
    PENDING on next inbound — handler then sends greeting fresh."""
    old = datetime.now(timezone.utc) - timedelta(
        days=h.STALE_AFTER_DAYS + 5
    )
    p = _patient(
        step=h.NEEDS_NAME,
        onboarding_retry_count=2,
        onboarding_step_at=old,
    )
    captured = _patch(monkeypatch, patient=p)
    delta = await h.handle_onboarding(
        patient_phone="9100", new_user_text="hello again"
    )
    # Two updates: stale-reset (→ PENDING) then state-advance (→ NEEDS_NAME).
    assert len(captured["updates"]) == 2
    assert captured["updates"][0][1]["step"] == h.PENDING
    assert captured["updates"][1][1]["step"] == h.NEEDS_NAME
    # Greeting was sent.
    assert delta["audit_reasons"] == ["onboarding_greeting"]
    # Retry counter cleared by the reset.
    assert p.onboarding_retry_count == 0


async def test_fresh_patient_within_window_does_not_reset(monkeypatch):
    """Recent transitions don't trigger reset — patient keeps progressing
    through their existing step."""
    recent = datetime.now(timezone.utc) - timedelta(days=2)
    p = _patient(step=h.NEEDS_NAME, onboarding_step_at=recent)
    captured = _patch(monkeypatch, patient=p)
    delta = await h.handle_onboarding(
        patient_phone="9100", new_user_text="Asha Mehta"
    )
    # One update only: state advance, no reset-to-pending.
    assert len(captured["updates"]) == 1
    assert captured["updates"][0][1]["step"] == h.NEEDS_COHORTS
    assert "Asha Mehta" in delta["response_body"]


async def test_legacy_null_step_at_does_not_reset(monkeypatch):
    """Legacy rows pre-migration have NULL ``onboarding_step_at`` —
    the handler must NOT treat that as stale (would re-greet every
    legacy row on next inbound)."""
    p = _patient(step=h.NEEDS_NAME, onboarding_step_at=None)
    captured = _patch(monkeypatch, patient=p)
    delta = await h.handle_onboarding(
        patient_phone="9100", new_user_text="Asha Mehta"
    )
    # Just the state advance — no PENDING reset in front of it.
    assert len(captured["updates"]) == 1
    assert captured["updates"][0][1]["step"] == h.NEEDS_COHORTS
    assert "Asha Mehta" in delta["response_body"]


async def test_stale_done_patient_is_not_reset(monkeypatch):
    """A DONE patient with an old step_at must NOT be re-greeted —
    the stale-reset gate only applies to active onboarding states."""
    old = datetime.now(timezone.utc) - timedelta(
        days=h.STALE_AFTER_DAYS + 100
    )
    p = _patient(step=h.DONE, onboarding_step_at=old)
    captured = _patch(monkeypatch, patient=p)
    out = await h.handle_onboarding(patient_phone="9100", new_user_text="hi")
    assert out is None
    assert captured["updates"] == []

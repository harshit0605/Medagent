"""Unit tests for the caregiver inbound consent handler.

Mocks the DB session + caregivers repo so we cover only the handler's
state-machine logic. Integration with the agent workflow + repo is
covered in tests/integration.
"""

from __future__ import annotations

import types

from services.orchestrator import caregiver_handler


class _NoopAsyncSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return None

    async def commit(self):
        return None


def _patch_session(monkeypatch):
    monkeypatch.setattr(
        caregiver_handler,
        "get_sessionmaker",
        lambda: lambda: _NoopAsyncSession(),
    )


def _caregiver(
    *,
    id=1,
    phone="91+caregiver",
    consent_status="pending",
    active=True,
):
    return types.SimpleNamespace(
        id=id, phone=phone, consent_status=consent_status, active=active
    )


def _stub_repos(
    monkeypatch,
    *,
    caregiver_by_id=None,
    pending_caregiver=None,
):
    captured = {"calls": []}

    async def get_by_id(_db, _id):
        return caregiver_by_id

    async def find_pending(_db, _phone):
        return pending_caregiver

    async def confirm(_db, cid, *, confirmed_by):
        captured["calls"].append(("confirm_consent", cid, confirmed_by))
        if caregiver_by_id is not None:
            caregiver_by_id.consent_status = "confirmed"
            return caregiver_by_id
        if pending_caregiver is not None:
            pending_caregiver.consent_status = "confirmed"
            return pending_caregiver
        return None

    async def decline(_db, cid):
        captured["calls"].append(("decline_consent", cid))
        if caregiver_by_id is not None:
            caregiver_by_id.consent_status = "declined"
            return caregiver_by_id
        if pending_caregiver is not None:
            pending_caregiver.consent_status = "declined"
            return pending_caregiver
        return None

    monkeypatch.setattr(caregiver_handler.caregivers_repo, "get", get_by_id)
    monkeypatch.setattr(
        caregiver_handler.caregivers_repo, "confirm_consent", confirm
    )
    monkeypatch.setattr(
        caregiver_handler.caregivers_repo, "decline_consent", decline
    )

    # The pending-by-phone lookup is the module-level helper.
    async def find_pending_local(_db, _phone):
        return pending_caregiver

    monkeypatch.setattr(
        caregiver_handler, "_find_pending_caregiver", find_pending_local
    )
    return captured


# ---- Recognisers --------------------------------------------------------


def test_recogniser_matches_marker_form():
    assert caregiver_handler.looks_like_caregiver_action(
        "[caregiver-action] confirm caregiver_id=12"
    )
    assert caregiver_handler.looks_like_caregiver_action(
        "[caregiver-action] decline caregiver_id=7"
    )


def test_recogniser_matches_yes_no_variants():
    assert caregiver_handler.looks_like_caregiver_action("YES")
    assert caregiver_handler.looks_like_caregiver_action("yes")
    assert caregiver_handler.looks_like_caregiver_action("y")
    assert caregiver_handler.looks_like_caregiver_action("confirm")
    assert caregiver_handler.looks_like_caregiver_action("I agree")
    assert caregiver_handler.looks_like_caregiver_action("NO")
    assert caregiver_handler.looks_like_caregiver_action("decline")
    assert caregiver_handler.looks_like_caregiver_action("opt out")


def test_recogniser_rejects_recap_copy_and_freeform():
    """``OK`` is the recap-ack copy — caregiver matchers must NOT
    swallow it. Same for unrelated chatter."""
    assert not caregiver_handler.looks_like_caregiver_action("OK")
    assert not caregiver_handler.looks_like_caregiver_action("got it")
    assert not caregiver_handler.looks_like_caregiver_action("thanks")
    assert not caregiver_handler.looks_like_caregiver_action(
        "I have a question"
    )
    assert not caregiver_handler.looks_like_caregiver_action("")
    assert not caregiver_handler.looks_like_caregiver_action(None)


# ---- Handler state-machine ---------------------------------------------


async def test_yes_confirms_pending_caregiver(monkeypatch):
    cg = _caregiver(id=42)
    captured = _stub_repos(monkeypatch, pending_caregiver=cg)
    _patch_session(monkeypatch)

    delta = await caregiver_handler.handle_caregiver_action(
        sender_phone="91+caregiver", new_user_text="YES"
    )
    assert delta is not None
    assert delta["audit_reasons"] == ["caregiver_action_confirmed"]
    actions = [c[0] for c in captured["calls"]]
    assert "confirm_consent" in actions
    # Confirmed-by tag is the wire-trust marker — distinct from
    # ops-recorded verbal consent which uses a clinician handle.
    assert any(
        c[0] == "confirm_consent" and c[2] == "caregiver_yes_reply"
        for c in captured["calls"]
    )


async def test_no_declines_pending_caregiver(monkeypatch):
    cg = _caregiver(id=42)
    captured = _stub_repos(monkeypatch, pending_caregiver=cg)
    _patch_session(monkeypatch)

    delta = await caregiver_handler.handle_caregiver_action(
        sender_phone="91+caregiver", new_user_text="No"
    )
    assert delta is not None
    assert delta["audit_reasons"] == ["caregiver_action_declined"]
    assert any(c[0] == "decline_consent" for c in captured["calls"])


async def test_marker_form_routes_via_id_hint(monkeypatch):
    cg = _caregiver(id=99)
    captured = _stub_repos(monkeypatch, caregiver_by_id=cg)
    _patch_session(monkeypatch)

    delta = await caregiver_handler.handle_caregiver_action(
        sender_phone="91+caregiver",
        new_user_text="[caregiver-action] confirm caregiver_id=99",
    )
    assert delta is not None
    assert delta["audit_reasons"] == ["caregiver_action_confirmed"]
    confirm_calls = [c for c in captured["calls"] if c[0] == "confirm_consent"]
    assert len(confirm_calls) == 1
    assert confirm_calls[0][1] == 99


async def test_yes_with_no_pending_returns_none_for_fallthrough(monkeypatch):
    """No pending caregiver → handler returns None so the orchestrator
    falls through to detect_intent. Critical: a stray "YES" elsewhere
    in the flow shouldn't get swallowed by the caregiver path."""
    _stub_repos(monkeypatch, pending_caregiver=None)
    _patch_session(monkeypatch)

    out = await caregiver_handler.handle_caregiver_action(
        sender_phone="91+random", new_user_text="YES"
    )
    assert out is None


async def test_phone_mismatch_in_marker_path_refused(monkeypatch):
    """Marker form sends caregiver_id; we still verify the caregiver
    row's phone matches the inbound. Defensive — a malicious payload
    could try to confirm someone else's caregiver."""
    cg = _caregiver(id=99, phone="91+different")
    _stub_repos(monkeypatch, caregiver_by_id=cg)
    _patch_session(monkeypatch)

    delta = await caregiver_handler.handle_caregiver_action(
        sender_phone="91+attacker",
        new_user_text="[caregiver-action] confirm caregiver_id=99",
    )
    assert delta is not None
    assert delta["audit_reasons"] == ["caregiver_action_phone_mismatch"]


async def test_already_confirmed_is_idempotent(monkeypatch):
    """A re-tap of YES on an already-confirmed caregiver returns a
    helpful message and does NOT mutate state again."""
    cg = _caregiver(id=42, consent_status="confirmed")
    captured = _stub_repos(monkeypatch, pending_caregiver=None, caregiver_by_id=cg)
    _patch_session(monkeypatch)

    # Marker form so we hit the by-id path with the already-confirmed row.
    delta = await caregiver_handler.handle_caregiver_action(
        sender_phone="91+caregiver",
        new_user_text="[caregiver-action] confirm caregiver_id=42",
    )
    assert delta is not None
    assert delta["audit_reasons"] == ["caregiver_action_already_confirmed"]
    # No mutation calls — the row already says confirmed.
    assert not any(c[0] == "confirm_consent" for c in captured["calls"])
    assert not any(c[0] == "decline_consent" for c in captured["calls"])

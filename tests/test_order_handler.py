"""Unit tests for the order/refill action marker parsers (no DB)."""

from __future__ import annotations

from services.orchestrator.order_handler import looks_like_order_action
from services.orchestrator.refill_handler import looks_like_refill_action


def test_order_action_recognition():
    assert looks_like_order_action("[order-action] sub_approve order_id=12")
    assert looks_like_order_action("[order-action] sub_decline order_id=3")
    assert not looks_like_order_action("[order-action] frobnicate order_id=1")
    assert not looks_like_order_action("sub_approve order_id=1")
    assert not looks_like_order_action("")
    assert not looks_like_order_action(None)


def test_refill_reorder_marker_recognized():
    # The reorder action was added alongside done/snoozed/help.
    assert looks_like_refill_action("[refill-action] reorder regimen_id=4")
    assert looks_like_refill_action("[refill-action] done regimen_id=4")
    assert not looks_like_refill_action("[refill-action] frobnicate regimen_id=4")

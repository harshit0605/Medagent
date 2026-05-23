"""Deterministic handler for order-action button taps (substitution approval).

When a fulfillment partner proposes a substitution on an order, we send the
patient a ``substitution_approval_v1`` template with Approve / Decline
buttons. The Next.js webhook rewrites a tap into a marker-prefixed text::

    [order-action] sub_approve order_id=12
    [order-action] sub_decline order_id=12

This module decodes it, applies the decision to the order, and replies. No
LLM round-trip — pure CRUD. Cross-patient safety: the order's patient_id is
verified against the inbound's resolved patient.
"""

from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)

_ORDER_ACTION_RE = re.compile(
    r"^\s*\[order-action\]\s+(?P<action>sub_approve|sub_decline)\s+"
    r"order_id\s*=\s*(?P<id>\d+)\s*$",
    re.I,
)


def looks_like_order_action(text: str | None) -> bool:
    return bool(text and _ORDER_ACTION_RE.match(text))


async def handle_order_action(
    *, patient_phone: str, new_user_text: str
) -> dict[str, Any] | None:
    """Apply a substitution approve/decline tap to an order. Returns a
    workflow delta (``response_body`` + ``audit_reasons``) or ``None`` when
    the text isn't an order action."""
    from app.db.repositories import orders as orders_repo
    from app.db.repositories import patients as patients_repo
    from app.db.session import get_sessionmaker

    match = _ORDER_ACTION_RE.match(new_user_text or "")
    if match is None:
        return None
    action = match.group("action").lower()
    order_id = int(match.group("id"))

    SessionLocal = get_sessionmaker()
    async with SessionLocal() as db:
        order = await orders_repo.get(db, order_id)
        if order is None:
            return {
                "response_body": (
                    "I couldn't find that order — it may have been removed."
                ),
                "audit_reasons": ["order_action_unknown_order"],
            }

        patient = await patients_repo.get_by_phone(db, patient_phone)
        if patient is None or order.patient_id != patient.id:
            log.warning(
                "order action refused: phone=%s order.patient_id=%s",
                patient_phone,
                order.patient_id,
            )
            return {
                "response_body": (
                    "I can only update orders for your own account."
                ),
                "audit_reasons": ["order_action_cross_patient_refused"],
            }

        if order.substitution_status != "proposed":
            return {
                "response_body": (
                    "There's no pending substitution to confirm on that order."
                ),
                "audit_reasons": ["order_action_no_pending_substitution"],
            }

        approved = action == "sub_approve"
        await orders_repo.resolve_substitution(
            db, order_id, approved=approved
        )
        await db.commit()

    sub_med = order.substitution_medication or "the substitute"
    if approved:
        body = (
            f"Thanks — your pharmacy will dispense {sub_med} in place of "
            f"{order.medication_name}. We'll keep your reminders on track."
        )
        audit = ["order_action_substitution_approved"]
    else:
        body = (
            f"Noted — we won't substitute {order.medication_name}. Our team "
            "will follow up on other options for you."
        )
        audit = ["order_action_substitution_declined"]
    log.info(
        "order %s substitution %s by patient %s",
        order_id,
        "approved" if approved else "declined",
        patient_phone,
    )
    return {"response_body": body, "audit_reasons": audit}

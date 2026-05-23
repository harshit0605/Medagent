"""Pharmacy / fulfillment-partner adapter (replaceable interface).

The refill execute layer needs to hand a reorder off to *some* fulfillment
partner. Integrations differ wildly (a deep-link to a pharmacy app, a REST
API, a manual ops queue), so this module defines a small ``PharmacyAdapter``
Protocol and ships a default deep-link/manual implementation. A real partner
is a drop-in: implement ``place_order`` and point ``get_pharmacy_adapter`` at
it (env-selectable, mirroring ``get_llm``).

No payments here — per SoT §11 we never take money in-chat; we route the
patient to the partner to complete checkout. We only track the order's
lifecycle so reminders/receipts stay coherent.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import quote_plus

from app.db.models import OrderStatus

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrderRequest:
    patient_phone: str
    medication_name: str
    dose: str | None = None
    quantity: str | None = None
    regimen_id: int | None = None


@dataclass(frozen=True)
class PharmacyResult:
    """What an adapter returns. ``status`` is the OrderStatus value the order
    should start in (``pending`` for manual/deep-link; a synchronous API
    partner might return ``processing``)."""

    partner: str
    partner_order_id: str | None = None
    deeplink: str | None = None
    status: str = OrderStatus.pending.value


@runtime_checkable
class PharmacyAdapter(Protocol):
    name: str

    async def place_order(self, req: OrderRequest) -> PharmacyResult:
        ...


class DeepLinkPharmacyAdapter:
    """Default adapter: no live API. Produces a partner name + an optional
    deep-link the patient taps to complete the order with the pharmacy.

    Config (env):
      - ``PHARMACY_PARTNER_NAME`` — partner label (default ``manual``).
      - ``PHARMACY_DEEPLINK_TEMPLATE`` — a URL with ``{med}`` / ``{qty}`` /
        ``{phone}`` placeholders. When unset, no deep-link is produced and the
        order is fulfilled manually by ops.
    """

    def __init__(self) -> None:
        self.name = os.getenv("PHARMACY_PARTNER_NAME", "manual")
        self._template = os.getenv("PHARMACY_DEEPLINK_TEMPLATE", "")

    async def place_order(self, req: OrderRequest) -> PharmacyResult:
        deeplink: str | None = None
        if self._template:
            try:
                deeplink = self._template.format(
                    med=quote_plus(req.medication_name or ""),
                    qty=quote_plus(req.quantity or ""),
                    phone=quote_plus(req.patient_phone or ""),
                )
            except (KeyError, IndexError, ValueError) as exc:
                # A malformed template shouldn't break a reorder — fall back to
                # manual fulfillment.
                log.warning("pharmacy deeplink template failed: %s", exc)
                deeplink = None
        return PharmacyResult(
            partner=self.name,
            partner_order_id=None,
            deeplink=deeplink,
            status=OrderStatus.pending.value,
        )


# Cached default adapter — cheap to build, but keep one per process so a
# future stateful adapter (connection pool, auth token) is reused.
_adapter: PharmacyAdapter | None = None


def get_pharmacy_adapter() -> PharmacyAdapter:
    """Return the configured pharmacy adapter. Swappable seam — a real
    integration replaces the default here (or via env dispatch)."""
    global _adapter
    if _adapter is None:
        _adapter = DeepLinkPharmacyAdapter()
    return _adapter

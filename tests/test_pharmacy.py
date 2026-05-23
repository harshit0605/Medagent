"""Unit tests for the pharmacy partner adapter (no DB)."""

from __future__ import annotations

from services.orchestrator import pharmacy
from services.orchestrator.pharmacy import (
    DeepLinkPharmacyAdapter,
    OrderRequest,
    PharmacyAdapter,
    get_pharmacy_adapter,
)


async def test_deeplink_adapter_no_template(monkeypatch):
    monkeypatch.delenv("PHARMACY_DEEPLINK_TEMPLATE", raising=False)
    monkeypatch.setenv("PHARMACY_PARTNER_NAME", "manual")
    adapter = DeepLinkPharmacyAdapter()
    result = await adapter.place_order(
        OrderRequest(patient_phone="p1", medication_name="Metformin")
    )
    assert result.partner == "manual"
    assert result.deeplink is None
    assert result.partner_order_id is None
    assert result.status == "pending"


async def test_deeplink_adapter_with_template(monkeypatch):
    monkeypatch.setenv("PHARMACY_PARTNER_NAME", "acme_rx")
    monkeypatch.setenv(
        "PHARMACY_DEEPLINK_TEMPLATE",
        "https://acme.example/order?med={med}&qty={qty}",
    )
    adapter = DeepLinkPharmacyAdapter()
    result = await adapter.place_order(
        OrderRequest(
            patient_phone="+9199",
            medication_name="Metformin 500mg",
            quantity="30 tabs",
        )
    )
    assert result.partner == "acme_rx"
    assert result.deeplink is not None
    # values are URL-encoded (quote_plus → spaces become +).
    assert "med=Metformin+500mg" in result.deeplink
    assert "qty=30+tabs" in result.deeplink


async def test_deeplink_adapter_malformed_template_falls_back(monkeypatch):
    monkeypatch.setenv("PHARMACY_DEEPLINK_TEMPLATE", "https://x/{unknown}")
    adapter = DeepLinkPharmacyAdapter()
    result = await adapter.place_order(
        OrderRequest(patient_phone="p", medication_name="m")
    )
    assert result.deeplink is None  # malformed → manual fulfillment


def test_get_pharmacy_adapter_returns_protocol(monkeypatch):
    monkeypatch.setattr(pharmacy, "_adapter", None)
    adapter = get_pharmacy_adapter()
    assert isinstance(adapter, PharmacyAdapter)

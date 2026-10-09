"""A crypto top-up invoice must be payable: it names a chain the deposit
watcher scans, an address to send to, and a non-zero amount."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from greencompute_gateway.application.billing_service import BillingService
from greencompute_gateway.infrastructure import price_feed
from greencompute_gateway.infrastructure.billing_repository import BillingRepository
from greencompute_gateway.transport import routes

TAO_ADDR = "5FFzLoiNKzXVgG4Rvr4wR2NeuymvFZTptCujX6abGPaUgqwN"


@pytest.fixture
def topup(monkeypatch):
    billing = BillingService(BillingRepository(database_url="sqlite:///:memory:", bootstrap=True))
    monkeypatch.setattr(routes, "require_api_key", lambda *a, **k: SimpleNamespace(user_id="u1"))
    monkeypatch.setattr(routes, "_get_billing", lambda: billing)
    monkeypatch.setattr(price_feed, "get_price", lambda currency: 300.0)
    monkeypatch.setenv("BILLING_DEPOSIT_TAO", TAO_ADDR)
    monkeypatch.setenv("BILLING_DEPOSIT_USDC_BASE", "0xabc")
    for name in ("BILLING_DEPOSIT_USDT", "BILLING_DEPOSIT_USDC", "BILLING_DEPOSIT_ALPHA"):
        monkeypatch.delenv(name, raising=False)
    return lambda currency, amount=5: routes.billing_topup_crypto(
        {"currency": currency, "amount_usd": amount}, authorization="Bearer k", x_api_key=None
    )


@pytest.mark.parametrize("bare", ["usdt", "usdc", "USDT"])
def test_bare_stablecoin_is_rejected_with_the_chain_options(topup, bare):
    with pytest.raises(HTTPException) as exc:
        topup(bare)
    assert exc.value.status_code == 400
    assert f"{bare.lower()}-base" in exc.value.detail and f"{bare.lower()}-eth" in exc.value.detail


def test_tao_invoice_has_address_and_amount(topup):
    inv = topup("tao", 6)
    assert inv["deposit_address"] == TAO_ADDR
    assert inv["amount_crypto"] == pytest.approx(0.02)


def test_alpha_pays_to_the_tao_coldkey(topup):
    assert topup("alpha")["deposit_address"] == TAO_ADDR


def test_chain_qualified_stablecoin_settles_one_to_one(topup):
    inv = topup("usdc-base", 25)
    assert inv["deposit_address"] == "0xabc"
    assert inv["amount_crypto"] == 25


def test_unconfigured_address_is_503_not_an_empty_invoice(topup):
    with pytest.raises(HTTPException) as exc:
        topup("usdt-eth")
    assert exc.value.status_code == 503


def test_missing_price_is_503_not_a_zero_amount_invoice(topup, monkeypatch):
    monkeypatch.setattr(price_feed, "get_price", lambda currency: 0.0)
    with pytest.raises(HTTPException) as exc:
        topup("tao")
    assert exc.value.status_code == 503

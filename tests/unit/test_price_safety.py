"""No made-up prices. When archive.minersunion.ai stopped resolving (10 Nov
2026), the alpha price fell back to a fixed $5 and the deposit watcher stopped.
A price that can't be read is now "unknown": invoices are refused, deposits
wait, and several endpoints can be listed."""
from __future__ import annotations

import sys
import time
import types

import pytest
from substrateinterface import Keypair

from greencompute_gateway.application.billing_service import BillingService
from greencompute_gateway.infrastructure import deposit_watcher as dw
from greencompute_gateway.infrastructure import price_feed as pf
from greencompute_gateway.infrastructure.billing_repository import BillingRepository
from greencompute_persistence import session_scope
from greencompute_persistence.orm import CryptoDepositORM, UserORM

TAO_ADDR = Keypair.create_from_uri("//GreenComputeDeposit").ss58_address


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    pf._cache.clear()
    for name in ("BILLING_PRICE_TAO", "BILLING_PRICE_ALPHA", "SUBTENSOR_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GREENCOMPUTE_SUBTENSOR_URL", "wss://dead.example, wss://alive.example")
    yield
    pf._cache.clear()


def test_endpoints_can_be_listed():
    assert pf.subtensor_urls() == ["wss://dead.example", "wss://alive.example"]
    assert pf._subtensor_url() == "wss://dead.example"


def test_alpha_price_tries_each_endpoint(monkeypatch):
    asked = []
    monkeypatch.setattr(pf, "_fetch_alpha_in_tao", lambda netuid, url: asked.append(url) or (0.03 if "alive" in url else None))
    pf._cache["tao"] = (200.0, time.time())
    assert pf.get_price("alpha") == pytest.approx(6.0)
    assert asked == ["wss://dead.example", "wss://alive.example"]


def test_no_alpha_price_is_unknown_not_five_dollars(monkeypatch):
    monkeypatch.setattr(pf, "_fetch_alpha_in_tao", lambda netuid, url: None)
    with pytest.raises(pf.PriceUnavailable):
        pf.get_price("alpha")


def test_a_recent_price_is_used_when_the_source_is_down(monkeypatch):
    monkeypatch.setattr(pf, "_fetch_alpha_in_tao", lambda netuid, url: None)
    pf._cache["alpha"] = (6.3, time.time() - 1800)  # 30 min old: stale but usable
    assert pf.get_price("alpha") == 6.3
    pf._cache["alpha"] = (6.3, time.time() - 2 * 3600)  # too old
    with pytest.raises(pf.PriceUnavailable):
        pf.get_price("alpha")


def test_no_tao_price_is_unknown_not_four_hundred(monkeypatch):
    import urllib.request

    def down(*a, **k):
        raise OSError("coingecko down")

    monkeypatch.setattr(urllib.request, "urlopen", down)
    with pytest.raises(pf.PriceUnavailable):
        pf.get_price("tao")


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.setenv("BILLING_DEPOSIT_ALPHA", TAO_ADDR)
    monkeypatch.setenv("BILLING_DEPOSIT_TAO", TAO_ADDR)

    def unknown(currency):
        raise pf.PriceUnavailable(f"no {currency.upper()} price available right now, try again shortly")

    monkeypatch.setattr(pf, "get_price", unknown)
    repo = BillingRepository(database_url="sqlite:///:memory:", bootstrap=True)
    with session_scope(repo.session_factory) as s:
        s.add(UserORM(user_id="alice", username="alice", email="a@x.io", balance_credits=0))
    return BillingService(repo)


def test_an_invoice_is_refused_without_a_price(svc):
    with pytest.raises(RuntimeError, match="no ALPHA price available right now"):
        svc.create_crypto_invoice("alice", "alpha", 25)


def _alpha_deposit(sender):
    return {
        "deposit_ref": "alpha:9:1", "currency": "alpha", "tx_hash": "0xa", "sender": sender,
        "deposit_address": TAO_ADDR, "amount_raw": 4_000_000_000, "decimals": 9,
        "block_number": 9, "usd_price": None,
    }


def test_a_deposit_that_cannot_be_valued_waits_instead_of_becoming_dust(svc):
    payer = Keypair.create_from_uri("//AliceAlpha")
    with pytest.raises(pf.PriceUnavailable):  # the watcher retries this block later
        svc.resolve_deposit(_alpha_deposit(payer.ss58_address))
    with session_scope(svc.repo.session_factory) as s:
        assert s.get(CryptoDepositORM, "alpha:9:1").status == "unmatched"


def test_a_wallet_claim_without_a_price_leaves_it_claimable(svc):
    payer = Keypair.create_from_uri("//AliceAlpha")
    with pytest.raises(pf.PriceUnavailable):
        svc.resolve_deposit(_alpha_deposit(payer.ss58_address))
    assert svc.claim_wallet_deposits("alice", "bittensor", payer.ss58_address) == []
    with session_scope(svc.repo.session_factory) as s:
        assert s.get(CryptoDepositORM, "alpha:9:1").status == "unmatched"
    assert svc.get_balance("alice")["balance_credits"] == 0


def test_the_watcher_tries_each_endpoint_and_stays_quiet_when_none_answers(monkeypatch, caplog):
    tried = []

    class Fake:
        def __init__(self, url, **kw):
            tried.append(url)
            raise OSError("No address associated with hostname")

    monkeypatch.setitem(sys.modules, "substrateinterface", types.SimpleNamespace(SubstrateInterface=Fake))
    monkeypatch.setattr(dw, "_deposit_addresses", lambda repo, cur: {"tao": {TAO_ADDR}, "alpha": {TAO_ADDR}})
    monkeypatch.setattr(dw, "_kv_get", lambda repo, key: "100")
    moved = []
    monkeypatch.setattr(dw, "_kv_set", lambda repo, key, value: moved.append(value))
    monkeypatch.setattr(dw, "_service", lambda repo: None)
    assert dw.scan_bittensor(object()) == 0
    assert tried == ["wss://dead.example", "wss://alive.example"]
    assert moved == []  # the cursor stays where it was
    assert "no endpoint reachable" in caplog.text and "Traceback" not in caplog.text

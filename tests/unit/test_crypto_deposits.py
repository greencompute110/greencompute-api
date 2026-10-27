"""Whose payment is it? Deposit addresses are shared, so the watcher must tell
users apart by a unique invoice amount or a verified sending wallet, and
must never credit one transfer twice."""
import sys
import types
from datetime import UTC, datetime, timedelta

import pytest
from eth_keys import keys
from eth_utils import keccak
from sqlalchemy.exc import IntegrityError
from substrateinterface import Keypair, KeypairType

from greencompute_persistence import session_scope
from greencompute_persistence.orm import CryptoDepositORM, CryptoInvoiceORM, UserORM
from greencompute_protocol import CryptoInvoice
from greencompute_gateway.application import billing_service as bs
from greencompute_gateway.application.billing_service import BillingService
from greencompute_gateway.infrastructure import deposit_watcher as dw
from greencompute_gateway.infrastructure import price_feed
from greencompute_gateway.infrastructure.billing_repository import (
    BillingRepository,
    WalletLinkedElsewhere,
)

TAO_ADDR = Keypair.create_from_uri("//GreenComputeDeposit").ss58_address
USDC_ADDR = "0x" + "ab" * 20
PRICES = {"tao": 300.0, "alpha": 5.0}


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.setenv("BILLING_DEPOSIT_TAO", TAO_ADDR)
    monkeypatch.setenv("BILLING_DEPOSIT_USDC_BASE", USDC_ADDR)
    for name in ("BILLING_DEPOSIT_ALPHA", "BILLING_DEPOSIT_USDC", "BILLING_DEPOSIT_USDT",
                 "BILLING_DEPOSIT_USDT_BASE", "BILLING_DEPOSIT_USDT_ETH", "BILLING_DEPOSIT_USDC_ETH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GREENCOMPUTE_BITTENSOR_NETUID", "110")
    monkeypatch.setattr(price_feed, "get_price", lambda c: PRICES.get(c, 0.0))
    repo = BillingRepository(database_url="sqlite:///:memory:", bootstrap=True)
    with session_scope(repo.session_factory) as s:
        for uid in ("alice", "bob", "carol"):
            s.add(UserORM(user_id=uid, username=uid, email=f"{uid}@x.io", balance_credits=0))
    return BillingService(repo)


def balance(svc, user):
    return svc.repo.get_balance(user)


def deposit(ref, currency="usdc-base", amount_raw=0, sender="0x" + "99" * 20, tx="0xtx"):
    bittensor = currency in ("tao", "alpha")
    return {
        "deposit_ref": ref,
        "currency": currency,
        "tx_hash": tx,
        "sender": sender,
        "deposit_address": TAO_ADDR if bittensor else USDC_ADDR,
        "amount_raw": amount_raw,
        "decimals": 9 if bittensor else 6,
        "block_number": 1,
        "usd_price": PRICES.get(currency, 1.0),
    }


def invoice_units(svc, invoice_id):
    with session_scope(svc.repo.session_factory) as s:
        return s.get(CryptoInvoiceORM, invoice_id).amount_units


def status_of(svc, invoice_id):
    with session_scope(svc.repo.session_factory) as s:
        return s.get(CryptoInvoiceORM, invoice_id).status


# --- unique amounts ---------------------------------------------------------


def test_same_usd_invoices_get_different_exact_amounts(svc):
    a = svc.create_crypto_invoice("alice", "usdc-base", 20)
    b = svc.create_crypto_invoice("bob", "usdc-base", 20)
    ua, ub = invoice_units(svc, a["invoice_id"]), invoice_units(svc, b["invoice_id"])
    assert ua != ub
    for units, inv in ((ua, a), (ub, b)):
        assert 20_000_000 < units < 20_010_000  # the extra is under a cent
        assert inv["amount_to_send"] == f"{units // 10**6}.{units % 10**6:06d}"


def test_two_same_amount_invoices_each_get_their_own_payment(svc):
    """The question that started this: Alice and Bob both open $20 invoices;
    Bob pays first. Bob, not Alice (who opened first), must be credited."""
    a = svc.create_crypto_invoice("alice", "usdc-base", 20)
    b = svc.create_crypto_invoice("bob", "usdc-base", 20)

    assert svc.resolve_deposit(deposit("usdc-base:0xb:0", amount_raw=invoice_units(svc, b["invoice_id"]))) == "invoice"
    assert (balance(svc, "alice"), balance(svc, "bob")) == (0, 2000)
    assert status_of(svc, a["invoice_id"]) == "pending"

    assert svc.resolve_deposit(deposit("usdc-base:0xa:0", amount_raw=invoice_units(svc, a["invoice_id"]))) == "invoice"
    assert (balance(svc, "alice"), balance(svc, "bob")) == (2000, 2000)


def test_tao_exact_amount_tolerates_float_rounding_in_wallets(svc):
    inv = svc.create_crypto_invoice("alice", "tao", 30)
    units = invoice_units(svc, inv["invoice_id"])
    # A wallet that converts "0.100017" through a float can land a rao short.
    assert svc.resolve_deposit(deposit("tao:5:1", "tao", units * 1000 - 1, sender="5Payer")) == "invoice"
    assert balance(svc, "alice") == 3000


def test_open_invoices_per_user_are_capped(svc):
    for _ in range(bs.MAX_PENDING_INVOICES_PER_USER):
        svc.create_crypto_invoice("alice", "usdc-base", 5)
    with pytest.raises(bs.TooManyOpenInvoices):
        svc.create_crypto_invoice("alice", "usdc-base", 5)
    svc.create_crypto_invoice("bob", "usdc-base", 5)  # others unaffected


def test_database_refuses_two_open_invoices_with_one_amount(svc):
    """Backstop for two requests racing to the same amount."""
    def make():
        inv = CryptoInvoice(user_id="alice", currency="usdc-base", amount_crypto=7.000001,
                            amount_usd=7, total_credits=700, deposit_address=USDC_ADDR,
                            expires_at=datetime.now(UTC) + timedelta(minutes=30))
        svc.repo.create_crypto_invoice(inv, amount_units=7_000_001)
    make()
    with pytest.raises(IntegrityError):
        make()


# --- resolution order ---------------------------------------------------------


def _link(svc, user, keypair):
    ch = svc.wallet_challenge(user, "bittensor", keypair.ss58_address)
    return svc.link_wallet(user, "bittensor", keypair.ss58_address, ch["issued_at"],
                           "0x" + keypair.sign(ch["message"]).hex())


def test_verified_wallet_pays_without_an_invoice(svc):
    payer = Keypair.create_from_uri("//AliceAgent")
    _link(svc, "alice", payer)
    # 0.5 TAO at $300, no invoice.
    assert svc.resolve_deposit(deposit("tao:9:1", "tao", 500_000_000, sender=payer.ss58_address)) == "wallet"
    assert balance(svc, "alice") == 15000


def test_alpha_from_a_verified_wallet_gets_the_alpha_bonus(svc):
    payer = Keypair.create_from_uri("//AliceAgent")
    _link(svc, "alice", payer)
    svc.resolve_deposit(deposit("alpha:9:1", "alpha", 2_000_000_000, sender=payer.ss58_address))
    assert balance(svc, "alice") == 1100  # 2 alpha at $5, +10%


def test_verified_wallet_wins_over_someone_elses_overpay_window(svc):
    """Bob has an open 20 USDC invoice; Alice's verified wallet sends 20.5
    USDC with no invoice. Under the old 5%-overpay rule Bob got it."""
    pk = keys.PrivateKey(bytes.fromhex("33" * 32))
    addr = pk.public_key.to_checksum_address()
    ch = svc.wallet_challenge("alice", "evm", addr)
    data = ch["message"].encode()
    sig = bytearray(pk.sign_msg_hash(keccak(b"\x19Ethereum Signed Message:\n" + str(len(data)).encode() + data)).to_bytes())
    sig[64] += 27
    svc.link_wallet("alice", "evm", addr, ch["issued_at"], "0x" + sig.hex())
    svc.create_crypto_invoice("bob", "usdc-base", 20)

    assert svc.resolve_deposit(deposit("usdc-base:0x1:0", amount_raw=20_500_000, sender=addr.lower())) == "wallet"
    assert (balance(svc, "alice"), balance(svc, "bob")) == (2050, 0)


def test_overpay_credits_only_when_unambiguous(svc):
    svc.create_crypto_invoice("alice", "usdc-base", 20)
    assert svc.resolve_deposit(deposit("usdc-base:0x1:0", amount_raw=20_300_000)) == "invoice_overpay"
    assert balance(svc, "alice") == 2030  # the full 20.3 USDC, not just the $20 invoiced

    svc.create_crypto_invoice("bob", "usdc-base", 20)
    svc.create_crypto_invoice("carol", "usdc-base", 20)
    # 20.3 fits both open invoices: nobody is credited, it waits for a claim.
    assert svc.resolve_deposit(deposit("usdc-base:0x2:0", amount_raw=20_300_000)) == "unmatched"
    assert (balance(svc, "bob"), balance(svc, "carol")) == (0, 0)


def test_dust_is_ignored(svc):
    assert svc.resolve_deposit(deposit("tao:1:1", "tao", 10, sender="5Dust")) == "ignored"


def test_resolving_the_same_transfer_twice_credits_once(svc):
    inv = svc.create_crypto_invoice("alice", "usdc-base", 10)
    d = deposit("usdc-base:0x1:0", amount_raw=invoice_units(svc, inv["invoice_id"]))
    assert svc.resolve_deposit(d) == "invoice"
    assert svc.resolve_deposit(d) == "seen"
    assert balance(svc, "alice") == 1000


# --- verified wallets and claims ------------------------------------------------


def test_linking_a_wallet_claims_its_unmatched_payments_once(svc):
    payer = Keypair.create_from_uri("//BobLaptop")
    assert svc.resolve_deposit(deposit("tao:7:1", "tao", 100_000_000, sender=payer.ss58_address)) == "unmatched"

    linked = _link(svc, "bob", payer)
    assert linked["newly_linked"] is True
    assert [c["deposit_ref"] for c in linked["credited"]] == ["tao:7:1"]
    assert balance(svc, "bob") == 3000  # 0.1 TAO at $300

    again = _link(svc, "bob", payer)
    assert again["newly_linked"] is False and again["credited"] == []
    assert balance(svc, "bob") == 3000


def test_claim_window_is_thirty_days(svc):
    payer = Keypair.create_from_uri("//BobLaptop")
    svc.resolve_deposit(deposit("tao:7:1", "tao", 100_000_000, sender=payer.ss58_address))
    with session_scope(svc.repo.session_factory) as s:
        s.get(CryptoDepositORM, "tao:7:1").observed_at = datetime.now(UTC) - timedelta(days=31)
    assert _link(svc, "bob", payer)["credited"] == []


def test_a_wallet_has_one_owner(svc):
    payer = Keypair.create_from_uri("//Shared")
    _link(svc, "alice", payer)
    with pytest.raises(WalletLinkedElsewhere):
        _link(svc, "bob", payer)


def test_link_requires_the_wallets_own_signature(svc):
    victim = Keypair.create_from_uri("//Victim")
    attacker = Keypair.create_from_uri("//Attacker")
    ch = svc.wallet_challenge("bob", "bittensor", victim.ss58_address)
    with pytest.raises(bs.WalletProofError):
        svc.link_wallet("bob", "bittensor", victim.ss58_address, ch["issued_at"],
                        "0x" + attacker.sign(ch["message"]).hex())


def test_a_signature_for_one_account_cant_link_another(svc):
    payer = Keypair.create_from_uri("//AliceAgent")
    ch = svc.wallet_challenge("alice", "bittensor", payer.ss58_address)
    sig = "0x" + payer.sign(ch["message"]).hex()
    with pytest.raises(bs.WalletProofError):
        svc.link_wallet("bob", "bittensor", payer.ss58_address, ch["issued_at"], sig)


def test_challenge_expires(svc, monkeypatch):
    payer = Keypair.create_from_uri("//AliceAgent")
    ch = svc.wallet_challenge("alice", "bittensor", payer.ss58_address)
    sig = "0x" + payer.sign(ch["message"]).hex()
    later = datetime.now(UTC) + bs.WALLET_CHALLENGE_TTL + timedelta(minutes=1)
    monkeypatch.setattr(bs, "datetime", types.SimpleNamespace(now=lambda tz=None: later, fromisoformat=datetime.fromisoformat))
    with pytest.raises(bs.WalletProofError):
        svc.link_wallet("alice", "bittensor", payer.ss58_address, ch["issued_at"], sig)


# --- admin overlap ----------------------------------------------------------------


def test_admin_confirm_then_wallet_link_does_not_pay_twice(svc):
    payer = Keypair.create_from_uri("//BobLaptop")
    inv = svc.create_crypto_invoice("bob", "tao", 30)
    # Bob pays far too little (under half the invoice); the watcher logs it unmatched.
    svc.resolve_deposit(deposit("tao:7:1", "tao", 40_000_000, sender=payer.ss58_address, tx="0xabc"))
    # Support confirms his invoice by hand against that transaction...
    assert svc.confirm_crypto_deposit(inv["invoice_id"], "0xabc")["credited"]
    # ...and later he links the wallet: nothing more is credited.
    assert _link(svc, "bob", payer)["credited"] == []
    assert balance(svc, "bob") == 3000


def test_watcher_seeing_a_transfer_after_the_admin_confirmed_it(svc):
    inv = svc.create_crypto_invoice("bob", "tao", 30)
    svc.confirm_crypto_deposit(inv["invoice_id"], "0xABC")
    other = svc.create_crypto_invoice("carol", "tao", 30)
    # The same transfer happens to match Carol's open invoice exactly.
    d = deposit("tao:7:1", "tao", invoice_units(svc, other["invoice_id"]) * 1000, tx="0xabc")
    assert svc.resolve_deposit(d) == "seen"
    assert (balance(svc, "bob"), balance(svc, "carol")) == (3000, 0)


def test_transfer_credited_before_the_deposit_log_existed_is_not_unmatched(svc):
    """The new bittensor cursor re-scans recent blocks the old watcher already
    credited; those transfers must not land in the claimable/unmatched list."""
    payer = Keypair.create_from_uri("//AliceOldPayment")
    inv = svc.create_crypto_invoice("alice", "tao", 30)
    units = invoice_units(svc, inv["invoice_id"])
    # What the previous watcher did: credit the invoice by deposit_ref, no deposit row.
    svc.repo.confirm_and_credit_invoice(inv["invoice_id"], "0xold", "TAO deposit", deposit_ref="tao:9:4")
    assert svc.resolve_deposit(deposit("tao:9:4", "tao", units * 1000, sender=payer.ss58_address, tx="0xold")) == "seen"
    logged = svc.repo.get_deposit("tao:9:4")
    assert (logged["status"], logged["user_id"], logged["invoice_id"]) == ("credited", "alice", inv["invoice_id"])
    assert svc.repo.list_deposits_for_admin(status="unmatched") == []
    assert _link(svc, "bob", payer)["credited"] == []
    assert (balance(svc, "alice"), balance(svc, "bob")) == (3000, 0)


def test_admin_assigns_an_unmatched_deposit_once(svc):
    svc.resolve_deposit(deposit("usdc-base:0x1:0", amount_raw=12_340_000))
    assert svc.assign_deposit("usdc-base:0x1:0", "carol")["credited"]
    assert svc.assign_deposit("usdc-base:0x1:0", "alice")["already_credited"]
    assert (balance(svc, "carol"), balance(svc, "alice")) == (1234, 0)


# --- scanners, end to end ----------------------------------------------------------

HEAD = 9_300_000


class FakeSubstrate:
    """The chain RPC; signature checks keep using the real Keypair."""

    events: dict = {}
    requested: list = []

    def __init__(self, **_kwargs):
        pass

    def get_chain_head(self):
        return "head"

    def get_block_number(self, _hash):
        return HEAD

    def get_block_hash(self, n):
        FakeSubstrate.requested.append(n)
        return n

    def get_events(self, block_hash):
        return FakeSubstrate.events.get(block_hash, [])

    def get_block(self, block_hash):
        ex = types.SimpleNamespace(extrinsic_hash=bytes.fromhex(f"{block_hash:064x}"))
        return {"extrinsics": [ex] * 8}

    def close(self):
        pass


FAKE_SUBSTRATE_MODULE = types.SimpleNamespace(
    SubstrateInterface=FakeSubstrate, Keypair=Keypair, KeypairType=KeypairType
)


def test_bittensor_scanner_runs_with_no_invoice_open(svc, monkeypatch):
    """Before: the TAO/alpha scanners only ran while an invoice was open, so a
    verified wallet paying without one would never have been seen."""
    monkeypatch.setitem(sys.modules, "substrateinterface", FAKE_SUBSTRATE_MODULE)
    payer = Keypair.create_from_uri("//AliceAgent")
    _link(svc, "alice", payer)
    paid_at = HEAD - 15
    FakeSubstrate.events = {paid_at: [{
        "module_id": "Balances", "event_id": "Transfer", "extrinsic_idx": 3,
        "attributes": {"from": payer.ss58_address, "to": TAO_ADDR, "amount": 200_000_000},
    }]}
    FakeSubstrate.requested = []

    credited = sum(dw.scan_bittensor(svc.repo) for _ in range(10))

    assert credited == 1 and balance(svc, "alice") == 6000
    # First run starts ~6h back, not at genesis or a months-old cursor.
    assert min(FakeSubstrate.requested) == HEAD - 10 - 1800 + 1
    row = svc.repo.get_deposit(f"tao:{paid_at}:0")
    assert row["tx_hash"] == "0x" + f"{paid_at:064x}" and row["matched_by"] == "wallet"


def test_bittensor_scanner_credits_alpha_invoice_by_exact_amount(svc, monkeypatch):
    monkeypatch.setitem(sys.modules, "substrateinterface", FAKE_SUBSTRATE_MODULE)
    inv = svc.create_crypto_invoice("bob", "alpha", 5)
    alpha_rao = invoice_units(svc, inv["invoice_id"]) * 1000
    FakeSubstrate.events = {HEAD - 12: [
        {"module_id": "SubtensorModule", "event_id": "StakeAdded", "extrinsic_idx": 2,
         "attributes": [TAO_ADDR, "5Hotkey", 16_486_440, alpha_rao, 110, 0]},
        {"module_id": "SubtensorModule", "event_id": "StakeTransferred", "extrinsic_idx": 2,
         "attributes": ["5BobsColdkey", TAO_ADDR, "5Hotkey", 110, 110, 16_486_440]},
    ]}
    sum(dw.scan_bittensor(svc.repo) for _ in range(10))
    assert status_of(svc, inv["invoice_id"]) == "confirmed"
    assert balance(svc, "bob") == 550


def _fake_evm(monkeypatch, logs_by_to: dict, head: int = 1_000):
    def rpc(url, method, params, timeout=15.0):
        if method == "eth_blockNumber":
            return hex(head)
        if method == "eth_getLogs":
            to = params[0]["topics"][2][-40:]
            return [l for l in logs_by_to.get(to, []) if params[0]["address"] == l["_contract"]]
        raise AssertionError(method)
    monkeypatch.setattr(dw, "_jsonrpc", rpc)


def _log(sender: str, amount: int, tx: str, idx: int = 0):
    return {
        "_contract": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC on Base
        "transactionHash": tx, "logIndex": hex(idx), "blockNumber": hex(900),
        "topics": [dw.ERC20_TRANSFER_TOPIC, "0x" + "0" * 24 + sender[2:], "0x" + "0" * 24 + USDC_ADDR[2:]],
        "data": hex(amount),
    }


def test_evm_scanner_tells_two_same_amount_invoices_apart(svc, monkeypatch):
    a = svc.create_crypto_invoice("alice", "usdc-base", 20)
    b = svc.create_crypto_invoice("bob", "usdc-base", 20)
    stray = "0x" + "77" * 20
    _fake_evm(monkeypatch, {USDC_ADDR[2:]: [
        _log("0x" + "11" * 20, invoice_units(svc, b["invoice_id"]), "0xbob"),
        _log("0x" + "22" * 20, invoice_units(svc, a["invoice_id"]), "0xalice"),
        _log(stray, 5_000_000, "0xstray"),  # no invoice, unverified wallet
    ]})

    assert dw.scan_evm_chain(svc.repo, "base") == 2
    assert (balance(svc, "alice"), balance(svc, "bob")) == (2000, 2000)
    stray_row = svc.repo.get_deposit("usdc-base:0xstray:0")
    assert stray_row["status"] == "unmatched" and stray_row["sender"] == stray


def test_evm_scanner_watches_the_address_with_no_invoice_open(svc, monkeypatch):
    _fake_evm(monkeypatch, {USDC_ADDR[2:]: [_log("0x" + "55" * 20, 3_000_000, "0xlate")]})
    dw.scan_evm_chain(svc.repo, "base")
    assert svc.repo.get_deposit("usdc-base:0xlate:0")["status"] == "unmatched"


# --- HTTP routes ------------------------------------------------------------------


@pytest.fixture
def api(svc, monkeypatch):
    from fastapi import HTTPException

    from greencompute_gateway.transport import routes

    who = {"user": "alice"}
    monkeypatch.setattr(routes, "require_api_key", lambda *a, **k: types.SimpleNamespace(user_id=who["user"]))
    monkeypatch.setattr(routes, "_get_billing", lambda: svc)
    auth = {"authorization": "Bearer k", "x_api_key": None}

    def call(name, *args, **kwargs):
        return getattr(routes, name)(*args, **kwargs, **auth)

    def status(name, *args, **kwargs):
        with pytest.raises(HTTPException) as exc:
            call(name, *args, **kwargs)
        return exc.value.status_code

    return types.SimpleNamespace(call=call, status=status, who=who)


def test_wallet_routes_link_list_and_unlink(api):
    payer = Keypair.create_from_uri("//AliceAgent")
    ch = api.call("billing_wallet_challenge", {"chain": "bittensor", "address": payer.ss58_address})
    linked = api.call("billing_link_wallet", {
        "chain": "bittensor", "address": payer.ss58_address,
        "issued_at": ch["issued_at"], "signature": "0x" + payer.sign(ch["message"]).hex(),
    })
    assert [w["address"] for w in api.call("billing_list_wallets")] == [payer.ss58_address]
    api.call("billing_unlink_wallet", linked["wallet_id"])
    assert api.call("billing_list_wallets") == []
    assert api.status("billing_unlink_wallet", linked["wallet_id"]) == 404


def test_link_route_errors(api):
    payer = Keypair.create_from_uri("//AliceAgent")
    ch = api.call("billing_wallet_challenge", {"chain": "bittensor", "address": payer.ss58_address})
    body = {"chain": "bittensor", "address": payer.ss58_address, "issued_at": ch["issued_at"],
            "signature": "0x" + payer.sign(ch["message"]).hex()}
    assert api.status("billing_link_wallet", {**body, "signature": "0x" + "00" * 64}) == 400
    assert api.status("billing_wallet_challenge", {"chain": "evm", "address": "nope"}) == 400
    api.call("billing_link_wallet", body)
    api.who["user"] = "bob"
    ch = api.call("billing_wallet_challenge", {"chain": "bittensor", "address": payer.ss58_address})
    assert api.status("billing_link_wallet", {**body, "issued_at": ch["issued_at"],
                                              "signature": "0x" + payer.sign(ch["message"]).hex()}) == 409


def test_topup_route_429_when_too_many_open(api):
    for _ in range(bs.MAX_PENDING_INVOICES_PER_USER):
        api.call("billing_topup_crypto", {"currency": "usdc-base", "amount_usd": 5})
    assert api.status("billing_topup_crypto", {"currency": "usdc-base", "amount_usd": 5}) == 429


def test_addresses_route_lists_configured_currencies(api):
    rows = {r["currency"]: r for r in api.call("billing_crypto_addresses")["currencies"]}
    assert rows["tao"]["deposit_address"] == TAO_ADDR and rows["tao"]["wallet_chain"] == "bittensor"
    assert rows["alpha"]["bonus_pct"] == 0.10
    assert rows["usdc-base"]["wallet_chain"] == "evm"
    assert "usdt-eth" not in rows  # not configured, not offered


def test_deposits_route_shows_only_the_callers_credited_deposits(api, svc):
    payer = Keypair.create_from_uri("//AliceAgent")
    _link(svc, "alice", payer)
    svc.resolve_deposit(deposit("tao:9:1", "tao", 100_000_000, sender=payer.ss58_address))
    svc.resolve_deposit(deposit("tao:9:2", "tao", 100_000_000, sender="5Stranger"))
    rows = api.call("billing_crypto_deposits")
    assert [r["deposit_ref"] for r in rows] == ["tao:9:1"]
    assert "user_id" not in rows[0]


def test_admin_assigns_by_username_or_email(api, svc, monkeypatch):
    svc.resolve_deposit(deposit("usdc-base:0x9:0", amount_raw=4_000_000))
    svc.resolve_deposit(deposit("usdc-base:0x9:1", amount_raw=6_000_000))
    assert api.call("billing_admin_assign_deposit", "usdc-base:0x9:0", {"user": "Carol"})["credited"]
    assert api.call("billing_admin_assign_deposit", "usdc-base:0x9:1", {"user": "bob@x.io"})["credited"]
    assert (balance(svc, "carol"), balance(svc, "bob")) == (400, 600)
    assert api.status("billing_admin_assign_deposit", "usdc-base:0x9:1", {"user": "bob"}) == 409
    assert api.status("billing_admin_assign_deposit", "usdc-base:0x9:1", {"user": "nobody"}) == 404


# --- payment reviews ----------------------------------------------------------------


def _expire(svc, invoice_id):
    with session_scope(svc.repo.session_factory) as s:
        s.get(CryptoInvoiceORM, invoice_id).status = "expired"


def test_user_requests_review_on_an_expired_invoice(api, svc):
    inv = svc.create_crypto_invoice("alice", "usdc-base", 20)
    _expire(svc, inv["invoice_id"])
    res = api.call("billing_request_review", inv["invoice_id"], {"tx_hash": "0xpaid", "note": "sent 20 from Binance"})
    assert res["review_status"] == "open"
    listed = {i["invoice_id"]: i for i in api.call("billing_list_crypto_invoices")}
    assert listed[inv["invoice_id"]]["review_status"] == "open"


def test_review_request_guards(api, svc):
    inv = svc.create_crypto_invoice("alice", "usdc-base", 20)
    assert api.status("billing_request_review", inv["invoice_id"], {"tx_hash": ""}) == 400
    api.who["user"] = "bob"
    assert api.status("billing_request_review", inv["invoice_id"], {"tx_hash": "0x1"}) == 404
    api.who["user"] = "alice"
    svc.resolve_deposit(deposit("usdc-base:0xok:0", amount_raw=invoice_units(svc, inv["invoice_id"])))
    assert api.status("billing_request_review", inv["invoice_id"], {"tx_hash": "0x1"}) == 409  # already confirmed


def test_admin_credits_the_logged_deposit_behind_a_review(api, svc):
    inv = svc.create_crypto_invoice("alice", "usdc-base", 20)
    svc.create_crypto_invoice("bob", "usdc-base", 20)
    # Alice rounds up to 21.5 USDC; Bob's invoice fits too, so nobody is credited.
    svc.resolve_deposit(deposit("usdc-base:0xpaid:0", amount_raw=21_500_000, tx="0xPAID"))
    _expire(svc, inv["invoice_id"])
    api.call("billing_request_review", inv["invoice_id"], {"tx_hash": "0xpaid"})

    [review] = api.call("billing_admin_list_reviews")
    assert review["username"] == "alice"
    assert [(d["deposit_ref"], d["status"], d["amount"]) for d in review["deposits"]] == [("usdc-base:0xpaid:0", "unmatched", 21.5)]

    res = api.call("billing_admin_resolve_review", inv["invoice_id"], {"action": "credit_deposit", "deposit_ref": "usdc-base:0xpaid:0"})
    assert res["credited_cents"] == 2150 and balance(svc, "alice") == 2150  # what actually arrived
    assert status_of(svc, inv["invoice_id"]) == "confirmed"
    assert api.call("billing_admin_list_reviews") == []
    assert api.status("billing_admin_resolve_review", inv["invoice_id"], {"action": "dismiss"}) == 409


def test_admin_confirms_an_expired_invoice_after_review_and_nothing_pays_twice(api, svc):
    payer = Keypair.create_from_uri("//AliceExchangeLater")
    inv = svc.create_crypto_invoice("alice", "tao", 30)
    _expire(svc, inv["invoice_id"])
    api.call("billing_request_review", inv["invoice_id"], {"tx_hash": "0xLATE"})
    res = api.call("billing_admin_resolve_review", inv["invoice_id"], {"action": "confirm_invoice"})
    assert res["credited_cents"] == 3000 and balance(svc, "alice") == 3000
    # The watcher logs the same transfer afterwards, from a wallet Alice then verifies.
    assert svc.resolve_deposit(deposit("tao:5:1", "tao", 100_000_000, sender=payer.ss58_address, tx="0xlate")) == "seen"
    assert _link(svc, "alice", payer)["credited"] == []
    assert balance(svc, "alice") == 3000


def test_dismissed_review_credits_nothing(api, svc):
    inv = svc.create_crypto_invoice("alice", "usdc-base", 20)
    api.call("billing_request_review", inv["invoice_id"], {"tx_hash": "0xnope"})
    assert api.call("billing_admin_resolve_review", inv["invoice_id"], {"action": "dismiss"})["review_status"] == "dismissed"
    assert balance(svc, "alice") == 0
    assert api.status("billing_admin_resolve_review", inv["invoice_id"], {"action": "confirm_invoice"}) == 409



# --- late payments, overpayments, alerts -----------------------------------------


def test_exact_payment_after_the_invoice_expired_is_still_credited(svc):
    inv = svc.create_crypto_invoice("alice", "tao", 30)  # 0.1 TAO at $300
    _expire(svc, inv["invoice_id"])
    PRICES["tao"] = 250.0  # TAO fell before the late payment arrived
    try:
        d = deposit("tao:8:1", "tao", invoice_units(svc, inv["invoice_id"]) * 1000)
        d["usd_price"] = 250.0
        assert svc.resolve_deposit(d) == "invoice_late"
    finally:
        PRICES["tao"] = 300.0
    # Credited at the price when it arrived: the invoice's exact amount at $250.
    assert balance(svc, "alice") == round(invoice_units(svc, inv["invoice_id"]) / 1e6 * 250 * 100)
    assert status_of(svc, inv["invoice_id"]) == "confirmed"


def test_late_matching_ends_after_24_hours(svc):
    inv = svc.create_crypto_invoice("alice", "usdc-base", 20)
    with session_scope(svc.repo.session_factory) as s:
        row = s.get(CryptoInvoiceORM, inv["invoice_id"])
        row.status = "expired"
        row.expires_at = datetime.now(UTC) - timedelta(hours=25)
    assert svc.resolve_deposit(deposit("usdc-base:0x1:0", amount_raw=invoice_units(svc, inv["invoice_id"]))) == "unmatched"


def test_new_invoices_do_not_reuse_an_amount_still_reserved_for_a_late_payment(svc, monkeypatch):
    first = svc.create_crypto_invoice("alice", "usdc-base", 20)
    _expire(svc, first["invoice_id"])
    reserved = invoice_units(svc, first["invoice_id"])
    # Force the random pick towards the reserved amount: it must still be skipped.
    monkeypatch.setattr(bs.random, "choice", lambda free: min(free, key=lambda e: abs(20_000_000 + e - reserved)))
    second = svc.create_crypto_invoice("bob", "usdc-base", 20)
    assert invoice_units(svc, second["invoice_id"]) != reserved


def test_alerts_go_to_slack_and_telegram_when_configured(monkeypatch):
    from greencompute_gateway.infrastructure import alerts

    sent = []
    monkeypatch.setattr(alerts, "_post", lambda url, body: sent.append((url, body)))
    monkeypatch.setattr(alerts.threading, "Thread", lambda target, args, **kw: types.SimpleNamespace(start=lambda: target(*args)))
    monkeypatch.setenv("GREENCOMPUTE_ALERT_SLACK_WEBHOOK_URL", "https://hooks.slack.test/T/B/x")
    monkeypatch.setenv("GREENCOMPUTE_ALERT_TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("GREENCOMPUTE_ALERT_TELEGRAM_CHAT_ID", "-10042")
    alerts.send_payment_alert("Unmatched payment: 7 USDC")
    assert sent == [
        ("https://hooks.slack.test/T/B/x", {"text": "Unmatched payment: 7 USDC"}),
        ("https://api.telegram.org/bot123:abc/sendMessage",
         {"chat_id": "-10042", "text": "Unmatched payment: 7 USDC", "disable_web_page_preview": True}),
    ]


def test_unmatched_payment_and_review_request_alert_the_team(api, svc, monkeypatch):
    from greencompute_gateway.infrastructure import alerts

    texts = []
    monkeypatch.setattr(alerts, "send_payment_alert", lambda text, **_: texts.append(text))
    svc.resolve_deposit(deposit("usdc-base:0xstray:0", amount_raw=7_000_000))
    inv = svc.create_crypto_invoice("alice", "usdc-base", 20)
    api.call("billing_request_review", inv["invoice_id"], {"tx_hash": "0xmine"})
    assert len(texts) == 2
    assert "Unmatched payment: 7 USDC-BASE" in texts[0]
    assert "review requested by alice" in texts[1] and "0xmine" in texts[1]


def test_no_alert_channel_configured_is_silent(monkeypatch):
    from greencompute_gateway.infrastructure import alerts

    for name in ("GREENCOMPUTE_ALERT_SLACK_WEBHOOK_URL", "GREENCOMPUTE_ALERT_TELEGRAM_BOT_TOKEN",
                 "GREENCOMPUTE_ALERT_TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(name, raising=False)
    assert alerts._requests("x") == []

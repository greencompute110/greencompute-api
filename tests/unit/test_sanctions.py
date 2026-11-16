"""Sanctions screening: reading the lists, keeping the last good copy when a
download fails, and refusing a sanctioned wallet at every door (a payment
from it, linking it, renting with it). No network: downloads and the oracle
are stand-ins."""
import types
from datetime import UTC, datetime, timedelta

import pytest
from substrateinterface import Keypair

from greencompute_gateway.application import sanctions
from greencompute_gateway.application.billing_service import BillingService, BlockedPaymentError, ReviewError, WalletBlocked
from greencompute_gateway.infrastructure import sanctions_lists
from greencompute_gateway.infrastructure.billing_repository import BillingRepository
from greencompute_persistence import session_scope
from greencompute_persistence.orm import (
    AccountEventORM,
    CryptoDepositORM,
    KeyValueStateORM,
    SanctionedAddressORM,
    UserORM,
    VerifiedWalletORM,
)

EVM_A = "0x" + "a1" * 20
EVM_B = "0x" + "b2" * 20
COLDKEY = Keypair.create_from_uri("//SanctionedColdkey")
USDC_ADDR = "0x" + "ab" * 20


def _sdn_csv(rows):
    # ent_num, name, type, program, title, call_sign, vess_type, tonnage, grt, vess_flag, vess_owner, remarks
    return "\n".join(
        ",".join([n, f'"{name}"'] + ['"-0- "'] * 9 + [f'"{remarks}"']) for n, name, remarks in rows
    ).encode()


# --- reading the lists ---------------------------------------------------------------


def test_ofac_remarks_are_joined_with_their_overflow_before_reading():
    """SDN.CSV cuts remarks at 1000 characters, sometimes mid-address; the
    rest is in SDN_COMMENTS.CSV."""
    full = EVM_A[:2] + EVM_A[2:].upper()
    sdn = _sdn_csv([
        ("100", "SPLIT, Entity", f"Digital Currency Address - ETH {full[:20]}"),
        ("200", "WHOLE, Person", f"Digital Currency Address - USDT {EVM_B}; alt. Digital Currency Address - XBT 1BoatSLRHtKNngkdXEeobR76b53LETtpyT"),
        ("300", "NOBODY", "Passport 12345"),
    ])
    comments = f'100,"{full[20:]}; Secondary sanctions risk"\n'.encode()
    out = sanctions_lists.parse_ofac_sdn(sdn, comments)
    assert out == {("evm", EVM_A): "SPLIT, Entity", ("evm", EVM_B): "WHOLE, Person"}


def test_ss58_keys_in_a_list_are_read_in_any_prefix_and_junk_is_not():
    polkadot = Keypair(public_key=COLDKEY.public_key, ss58_format=0).ss58_address
    text = f"Digital Currency Address - TAO {polkadot}; a base58 word that is no key 5" + "1" * 47
    assert sanctions_lists.addresses_in(text) == {("bittensor", COLDKEY.ss58_address)}


def test_the_uk_list_is_read_from_its_csv_with_names():
    raw = (
        "Report Date: 12-Nov-2026\n"
        "Last Updated,Unique ID,Name 6,Name 1,Other Information\n"
        f'04/08/2026,CYB0001,EVIL CORP,,"crypto: ETH: {EVM_A} (2) BNB: {EVM_B}"\n'
        "04/08/2026,AFG0001,SOMEONE ELSE,,nothing here\n"
    ).encode()
    assert sanctions_lists.parse_uk_list(raw) == {("evm", EVM_A): "EVIL CORP", ("evm", EVM_B): "EVIL CORP"}


def test_the_oracle_call_encodes_the_address_and_reads_the_bool(monkeypatch):
    seen = {}

    class Reply:
        def __init__(self, result):
            self.result = result

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            import json
            return json.dumps({"jsonrpc": "2.0", "id": 1, "result": self.result}).encode()

    def fake_urlopen(req, timeout):
        import json
        seen["body"] = json.loads(req.data)
        return Reply("0x" + "0" * 63 + "1")

    monkeypatch.setattr(sanctions_lists.urllib.request, "urlopen", fake_urlopen)
    assert sanctions_lists.oracle_is_sanctioned(EVM_A) is True
    call = seen["body"]["params"][0]
    assert call["to"] == "0x40C57923924B5c5c5455c48D93317139ADDaC8fb"
    assert call["data"] == "0xdf592f7d" + "0" * 24 + EVM_A[2:]


# --- refreshing ----------------------------------------------------------------------


@pytest.fixture
def db(tmp_path, monkeypatch):
    repo = BillingRepository(database_url=f"sqlite:///{tmp_path / 'gc.sqlite'}", bootstrap=True)
    alerts = []
    monkeypatch.setattr("greencompute_gateway.infrastructure.alerts.send_payment_alert",
                        lambda text, **kw: alerts.append(text))
    monkeypatch.setattr("greencompute_gateway.infrastructure.notify_website.notify_user", lambda *a, **k: None)
    sanctions._oracle_cache.clear()
    sanctions._oracle_failed_at.clear()
    return types.SimpleNamespace(repo=repo, sf=repo.session_factory, alerts=alerts)


def _downloads(files: dict):
    def download(url):
        value = files[url]
        if isinstance(value, Exception):
            raise value
        return value
    return download


def _files(ofac_addresses, uk_addresses):
    sdn_url, comments_url = sanctions_lists.ofac_urls()
    sdn = _sdn_csv([(str(i), f"PARTY {i}", f"Digital Currency Address - ETH {a}") for i, a in enumerate(ofac_addresses)])
    uk = ("Report Date: x\nLast Updated,Unique ID,Name 6,Other Information\n"
          + "".join(f'1,U{i},UK PARTY,"ETH {a}"\n' for i, a in enumerate(uk_addresses))).encode()
    return {sdn_url: sdn, comments_url: b"", sanctions_lists.uk_url(): uk}


def _many(n, prefix="c"):
    return [f"0x{prefix}{i:039x}" for i in range(n)]


def test_a_refresh_replaces_each_list_and_a_delisting_takes_effect(db):
    first = _many(25)
    assert sanctions.refresh(db.sf, download=_downloads(_files(first + [EVM_A], [EVM_B]))) == {
        "ofac_sdn": {"ok": True, "count": 26}, "uk_sanctions_list": {"ok": True, "count": 1}}
    assert sanctions.listed(db.sf, "evm", EVM_A) == "ofac_sdn"
    assert sanctions.listed(db.sf, "evm", EVM_B) == "uk_sanctions_list"
    sanctions.refresh(db.sf, download=_downloads(_files(first, [EVM_B])))
    assert sanctions.listed(db.sf, "evm", EVM_A) is None


def test_a_failed_or_implausible_download_keeps_the_last_good_copy(db):
    sanctions.refresh(db.sf, download=_downloads(_files(_many(25) + [EVM_A], [EVM_B])))
    sdn_url, _ = sanctions_lists.ofac_urls()
    broken = _files([], [])
    broken[sdn_url] = TimeoutError("treasury.gov timed out")
    out = sanctions.refresh(db.sf, download=_downloads(broken))
    assert out["ofac_sdn"]["ok"] is False and "timed out" in out["ofac_sdn"]["error"]
    assert out["uk_sanctions_list"]["ok"] is False  # 0 addresses: a format change, not a mass delisting
    assert sanctions.listed(db.sf, "evm", EVM_A) == "ofac_sdn"
    assert sanctions.listed(db.sf, "evm", EVM_B) == "uk_sanctions_list"
    state = sanctions.list_state(db.sf)
    assert state["ofac_sdn"]["addresses"] == 26 and "timed out" in state["ofac_sdn"]["last_error"]


def test_the_refresh_runs_when_a_copy_is_old_and_not_twice_at_once(db):
    every = timedelta(hours=24)
    assert sanctions._due(db.sf, every) == ("ofac_sdn", "uk_sanctions_list")
    assert sanctions._due(db.sf, every) == ()  # another process just claimed it
    sanctions.store_list(db.sf, "ofac_sdn", {("evm", EVM_A): ""})
    sanctions.store_list(db.sf, "uk_sanctions_list", {("evm", EVM_B): ""})
    with session_scope(db.sf) as s:
        s.delete(s.get(KeyValueStateORM, "sanctions:refresh_started_at"))
    assert sanctions._due(db.sf, every) == ()
    with session_scope(db.sf) as s:
        s.get(KeyValueStateORM, "sanctions:uk_sanctions_list:loaded_at").value = (
            datetime.now(UTC) - timedelta(hours=25)).isoformat()
    assert sanctions._due(db.sf, every) == ("uk_sanctions_list",)


def test_staff_hear_once_a_day_when_a_list_is_stale(db):
    sanctions._stale_alerted.clear()
    sanctions._alert_if_stale(db.sf)
    assert len(db.alerts) == 2 and any("new wallet sign-ups are refused" in a for a in db.alerts)
    sanctions._alert_if_stale(db.sf)
    assert len(db.alerts) == 2


# --- screening ------------------------------------------------------------------------


def test_screen_uses_the_lists_then_the_oracle_and_survives_an_oracle_outage(db, monkeypatch):
    sanctions.store_list(db.sf, "ofac_sdn", {("evm", EVM_A): "x"})
    monkeypatch.setenv("GREENCOMPUTE_SANCTIONS_ORACLE_ENABLED", "true")
    calls = []

    def oracle(address):
        calls.append(address)
        if address == "0x" + "ee" * 20:
            raise OSError("rpc down")
        return address == EVM_B

    monkeypatch.setattr(sanctions_lists, "oracle_is_sanctioned", oracle)
    assert sanctions.screen(db.sf, "evm", EVM_A) == "ofac_sdn"
    assert sanctions.screen(db.sf, "evm", EVM_B) == "chainalysis_oracle"
    assert sanctions.screen(db.sf, "evm", EVM_B) == "chainalysis_oracle"
    assert calls.count(EVM_B) == 1  # cached
    assert sanctions.screen(db.sf, "evm", "0x" + "ee" * 20) is None  # unknown: the lists decide
    before = len(calls)
    assert sanctions.screen(db.sf, "evm", "0x" + "dd" * 20) is None  # backing off: not asked again yet
    assert len(calls) == before
    assert sanctions.screen(db.sf, "bittensor", COLDKEY.ss58_address) is None
    monkeypatch.setenv("GREENCOMPUTE_SANCTIONS_ENABLED", "false")
    assert sanctions.screen(db.sf, "evm", EVM_A) is None


def test_only_new_accounts_fail_closed_before_the_lists_ever_load(db):
    assert sanctions.screen(db.sf, "evm", EVM_A) is None
    with pytest.raises(sanctions.ScreeningUnavailable):
        sanctions.screen(db.sf, "evm", EVM_A, new_account=True)
    sanctions.store_list(db.sf, "ofac_sdn", {("evm", EVM_B): ""})
    assert sanctions.screen(db.sf, "evm", EVM_A, new_account=True) is None


def _user_with_wallet(db, user_id, chain, address, **wallet):
    with session_scope(db.sf) as s:
        s.add(UserORM(user_id=user_id, username=user_id, email=f"{user_id}@x.io", balance_credits=0,
                      profile_metadata={}))
        s.add(VerifiedWalletORM(wallet_id=f"w-{user_id}", user_id=user_id, chain=chain, address=address,
                                created_at=datetime.now(UTC), **wallet))


def test_an_account_with_a_listed_or_oracle_flagged_wallet_is_blocked(db):
    _user_with_wallet(db, "listed", "evm", EVM_A)
    _user_with_wallet(db, "flagged", "evm", EVM_B, sanctions_hit="chainalysis_oracle")
    _user_with_wallet(db, "clean", "bittensor", COLDKEY.ss58_address)
    sanctions.store_list(db.sf, "ofac_sdn", {("evm", EVM_A): ""})
    assert sanctions.account_blocked(db.sf, "listed") == "list"
    assert sanctions.account_blocked(db.sf, "flagged") == "chainalysis_oracle"
    assert sanctions.account_blocked(db.sf, "clean") is None
    assert sanctions.account_blocked(db.sf, None) is None


# --- deposits ---------------------------------------------------------------------------


@pytest.fixture
def billing(db, monkeypatch):
    monkeypatch.setenv("BILLING_DEPOSIT_USDC_BASE", USDC_ADDR)
    with session_scope(db.sf) as s:
        s.add(UserORM(user_id="alice", username="alice", email="a@x.io", balance_credits=0, profile_metadata={}))
    sanctions.store_list(db.sf, "ofac_sdn", {("evm", EVM_A): "Listed"})
    return BillingService(db.repo)


def _deposit(ref, sender, amount_raw=10_000_000, tx="0xtx"):
    return {"deposit_ref": ref, "currency": "usdc-base", "tx_hash": tx, "sender": sender,
            "deposit_address": USDC_ADDR, "amount_raw": amount_raw, "decimals": 6, "block_number": 1,
            "usd_price": 1.0}


def _status(db, ref):
    with session_scope(db.sf) as s:
        row = s.get(CryptoDepositORM, ref)
        return row.status, row.matched_by


def test_a_payment_from_a_listed_sender_is_never_credited_even_for_an_exact_invoice(db, billing):
    inv = billing.create_crypto_invoice("alice", "usdc-base", 10)
    with session_scope(db.sf) as s:
        from greencompute_persistence.orm import CryptoInvoiceORM
        units = s.get(CryptoInvoiceORM, inv["invoice_id"]).amount_units
    assert billing.resolve_deposit(_deposit("usdc-base:0xbad:0", EVM_A.upper().replace("0X", "0x"), units)) == "blocked"
    assert db.repo.get_balance("alice") == 0
    assert _status(db, "usdc-base:0xbad:0") == ("blocked", "sanctions:ofac_sdn")
    assert len(db.alerts) == 1 and "NOT credited" in db.alerts[0]
    # Seen again by the next tick: still blocked, no second alert.
    assert billing.resolve_deposit(_deposit("usdc-base:0xbad:0", EVM_A, units)) == "seen"
    assert len(db.alerts) == 1
    # No path credits it afterwards: staff assigning it, a review, a confirm.
    assert billing.assign_deposit("usdc-base:0xbad:0", "alice") == {"blocked": True, "deposit_ref": "usdc-base:0xbad:0"}
    with pytest.raises(BlockedPaymentError):
        billing.confirm_crypto_deposit(inv["invoice_id"], "0xtx")
    assert db.repo.get_balance("alice") == 0
    with session_scope(db.sf) as s:
        assert s.query(AccountEventORM).count() == 0


def test_a_clean_payment_is_credited_and_emits_payment_credited(db, billing):
    db.repo.add_verified_wallet("alice", "evm", EVM_B)
    assert billing.resolve_deposit(_deposit("usdc-base:0xok:0", EVM_B, tx="0xok")) == "wallet"
    assert db.repo.get_balance("alice") == 1000
    with session_scope(db.sf) as s:
        [event] = s.query(AccountEventORM).all()
        assert (event.type, event.data["method"], event.data["matched_by"]) == ("payment.credited", "crypto", "wallet")


def test_a_verified_wallet_found_by_the_oracle_on_a_deposit_blocks_its_account(db, billing, monkeypatch):
    db.repo.add_verified_wallet("alice", "evm", EVM_B)
    monkeypatch.setenv("GREENCOMPUTE_SANCTIONS_ORACLE_ENABLED", "true")
    monkeypatch.setattr(sanctions_lists, "oracle_is_sanctioned", lambda a: a == EVM_B)
    assert billing.resolve_deposit(_deposit("usdc-base:0xo:0", EVM_B, tx="0xo")) == "blocked"
    assert sanctions.account_blocked(db.sf, "alice") == "chainalysis_oracle"
    assert "alice" in db.alerts[0] and "can no longer rent" in db.alerts[0]


def test_a_listed_wallet_cant_be_linked(db, billing):
    from eth_keys import keys
    from eth_utils import keccak

    key = keys.PrivateKey(bytes.fromhex("c3" * 32))
    address = key.public_key.to_checksum_address().lower()
    sanctions.store_list(db.sf, "ofac_sdn", {("evm", address): "Listed"})
    ch = billing.wallet_challenge("alice", "evm", address)
    data = ch["message"].encode()
    sig = key.sign_msg_hash(keccak(b"\x19Ethereum Signed Message:\n" + str(len(data)).encode() + data)).to_bytes()
    with pytest.raises(WalletBlocked):
        billing.link_wallet("alice", "evm", address, ch["issued_at"], "0x" + sig[:64].hex() + bytes([sig[64] + 27]).hex())
    assert db.repo.list_verified_wallets("alice") == []
    assert len(db.alerts) == 1


def test_a_review_cant_credit_a_blocked_deposit(db, billing):
    inv = billing.create_crypto_invoice("alice", "usdc-base", 10)
    billing.resolve_deposit(_deposit("usdc-base:0xr:0", EVM_A, tx="0xr"))
    with session_scope(db.sf) as s:
        from greencompute_persistence.orm import CryptoInvoiceORM
        row = s.get(CryptoInvoiceORM, inv["invoice_id"])
        row.review_status, row.tx_hash = "open", "0xr"
    with pytest.raises(ReviewError, match="sanctions"):
        billing.resolve_review(inv["invoice_id"], "credit_deposit", "usdc-base:0xr:0")
    with pytest.raises(ReviewError, match="sanctions"):
        billing.resolve_review(inv["invoice_id"], "confirm_invoice")
    assert db.repo.get_balance("alice") == 0


# --- renting ------------------------------------------------------------------------------


def test_an_account_with_a_sanctioned_wallet_cant_rent_or_resume(db, monkeypatch):
    from fastapi import HTTPException

    from greencompute_gateway.application.services import GatewayService, SanctionedAccountError
    from greencompute_gateway.transport import routes
    from greencompute_protocol import DeploymentCreateRequest

    _user_with_wallet(db, "renter", "evm", EVM_A)
    sanctions.store_list(db.sf, "ofac_sdn", {("evm", EVM_A): ""})
    workload = types.SimpleNamespace(workload_id="w", owner_user_id="renter", public=False, metadata={},
                                     requirements=types.SimpleNamespace(supported_gpu_models=[], gpu_count=1))
    cp = types.SimpleNamespace(repository=types.SimpleNamespace(get_workload=lambda _id: workload))
    svc = GatewayService(repository=types.SimpleNamespace(session_factory=db.sf), control_plane=cp)
    with pytest.raises(SanctionedAccountError):
        svc.create_deployment({"workload_id": "w"}, user_id="renter")
    monkeypatch.setattr(routes, "service", svc)
    monkeypatch.setattr(routes, "_enforce_key_limits", lambda *a, **k: None)
    key = types.SimpleNamespace(user_id="renter", admin=False, key_id="k", max_active_rentals=None,
                                daily_spend_limit_usd=None)
    with pytest.raises(HTTPException) as exc:
        routes._create_deployment(DeploymentCreateRequest(workload_id="w"), key)
    assert exc.value.status_code == 403 and exc.value.error_code == "wallet_blocked"
    assert exc.value.detail["message"] == sanctions.BLOCKED_MESSAGE
    with session_scope(db.sf) as s:
        assert s.query(SanctionedAddressORM).count() == 1

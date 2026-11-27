"""Paying with gamma, as Bittensor's Subnet services standard (and Lium, engy
and SayGM) do it: a hotkey spends SN110 gamma at our service hotkey, the
gamma watcher reads GammaSpent at the finalized head and credits the growth,
and the hotkey signs its requests (btauth/1).

Covered: the wire format against the standard's golden vectors and every
refusal; signed requests through the real ASGI app (body replayed, scopes,
placeholder bearer ignored, no account yet, payment-only link); crediting
exactly once, fractions of a cent, treasury buybacks, sanctions, a new
subnet counter; the watcher against a fake chain; the public documents; the
weekly limit (what is over it waits and is credited later, oldest first,
never lost and never ahead of gamma already waiting)."""
import asyncio
import hashlib
import json
import types
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from substrateinterface import Keypair, KeypairType

from greencompute_gateway.application import gamma as gamma_app
from greencompute_gateway.application import sanctions, wallet_auth
from greencompute_gateway.application.billing_service import BillingService
from greencompute_gateway.domain import gamma
from greencompute_gateway.infrastructure import btauth, gamma_watcher
from greencompute_gateway.infrastructure.billing_repository import BillingRepository
from greencompute_gateway.infrastructure.repository import GatewayRepository
from greencompute_gateway.main import app
from greencompute_gateway.transport import routes, security
from greencompute_persistence import CredentialStore, FixedWindowRateLimiter, session_scope
from greencompute_persistence.orm import (
    APIKeyORM,
    CryptoDepositORM,
    GammaSpendORM,
    GammaSpentTotalORM,
    KeyValueStateORM,
    LedgerEntryORM,
    UserORM,
    VerifiedWalletORM,
)

SERVICE = Keypair.create_from_uri("//GammaService")
TREASURY = Keypair.create_from_uri("//GammaTreasury")
CUSTOMER = Keypair.create_from_uri("//GammaCustomer")
OTHER = Keypair.create_from_uri("//GammaOther")
LISTED = Keypair.create_from_uri("//GammaListed")
THIRD = Keypair.create_from_uri("//GammaThird")
G = 10**9  # raw units per gamma


@pytest.fixture
def env(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'gc.sqlite'}"
    repo = GatewayRepository(database_url=url, bootstrap=True)
    billing = BillingService(BillingRepository(database_url=url))
    monkeypatch.setattr(routes.service, "repository", repo)
    monkeypatch.setattr(routes, "_get_billing", lambda: billing)
    monkeypatch.setattr(security, "credential_store", CredentialStore(session_factory=repo.session_factory))
    monkeypatch.setattr(security, "rate_limiter", FixedWindowRateLimiter())
    monkeypatch.setenv("GREENCOMPUTE_GAMMA_SERVICE_HOTKEYS", SERVICE.ss58_address)
    monkeypatch.setenv("GREENCOMPUTE_GAMMA_TREASURY_HOTKEYS", TREASURY.ss58_address)
    alerts = []
    monkeypatch.setattr("greencompute_gateway.infrastructure.alerts.send_payment_alert",
                        lambda text, **kw: alerts.append(text))
    btauth.nonce_store.clear()
    gamma_app._ALERTED.clear()
    gamma_app._status_cache.update(at=0.0, value=None)
    wallet_auth._ALERTED.clear()
    gamma_watcher._ticks["n"] = 0
    gamma_watcher._release_failures["n"] = 0
    # The lists have loaded (one listed hotkey), as they have in production.
    sanctions.store_list(repo.session_factory, sanctions.OFAC, {("bittensor", LISTED.ss58_address): "Listed Party"})
    return types.SimpleNamespace(sf=repo.session_factory, billing=billing, alerts=alerts)


def call(method, path, body=None, *, headers=None, raw=None, query=b""):
    """One request through the ASGI app; (status, json body)."""
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
    hdrs = [(b"content-type", b"application/json"), (b"host", b"api.test")]
    for name, value in (headers or {}).items():
        hdrs.append((name.lower().encode(), str(value).encode()))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
        "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": query,
        "headers": hdrs, "client": ("203.0.113.9", 1234), "server": ("api.test", 80),
    }
    sent = {"status": None, "body": b"", "headers": {}}
    chunks = [{"type": "http.request", "body": data, "more_body": False}]

    async def receive():
        return chunks.pop(0) if chunks else {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            sent["status"] = message["status"]
            sent["headers"] = {k.decode().lower(): v.decode() for k, v in message.get("headers", [])}
        elif message["type"] == "http.response.body":
            sent["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    call.last_headers = sent["headers"]
    return sent["status"], json.loads(sent["body"] or b"null")


def signed(method, path, body=None, *, key=CUSTOMER, receiver=None, query="", **extra_headers):
    raw = json.dumps(body).encode() if body is not None else b""
    target = f"{path}?{query}" if query else path
    headers = btauth.sign(key, method=method, target=target, body=raw, receiver=receiver or SERVICE.ss58_address)
    headers.update(extra_headers)
    return call(method, path, raw=raw, headers=headers, query=query.encode())


def credit(env, hotkey, total_gamma_raw, *, counter=0, block=100, coldkey=None, service=None):
    return gamma_app.credit_growth(
        env.sf, netuid=110, counter=counter, service=service or SERVICE.ss58_address, hotkey=hotkey,
        total_raw=total_gamma_raw, block=block, coldkey=coldkey, billing=env.billing,
    )


def balance_cents(env, user_id):
    with session_scope(env.sf) as s:
        return s.get(UserORM, user_id).balance_credits


def owner_of(env, address):
    with session_scope(env.sf) as s:
        return s.scalar(select(VerifiedWalletORM.user_id).where(VerifiedWalletORM.address == address))


# --- the wire format ----------------------------------------------------------------

GOLDEN_BODY = b'{"prompt": "hello"}'
BOB = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
NONCE = 1752076800000000000


def test_payload_matches_the_standards_golden_vectors():
    alice = Keypair.create_from_uri("//Alice")
    assert hashlib.sha256(GOLDEN_BODY).hexdigest() == "341c57448e531310fbbe83f44cea2a5e838bd9e8a6b82b269f01d0dbbc23c3cc"
    payload = btauth.build_payload(scheme="sr25519", method="post", target="/generate?stream=false",
                                   body=GOLDEN_BODY, nonce=NONCE, sender=alice.ss58_address, receiver=BOB)
    assert payload == (
        b"btauth/1\nsr25519\nPOST\n/generate?stream=false\n"
        b"341c57448e531310fbbe83f44cea2a5e838bd9e8a6b82b269f01d0dbbc23c3cc\n1752076800000000000\n"
        b"5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY\n5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"
    )
    # ed25519 signatures are deterministic, so the standard pins one: it
    # verifies over our payload, byte for byte.
    ed_alice = "5FA9nQDVg267DEd8m1ZypXLBnvN7SFxYwV7ndqSYGiN9TTpu"
    ed_payload = btauth.build_payload(scheme="ed25519", method="POST", target="/generate?stream=false",
                                      body=GOLDEN_BODY, nonce=NONCE, sender=ed_alice, receiver=BOB)
    pinned = bytes.fromhex(
        "1a6b631988bf1f7ea093ab9d5acf61f05c316d0b0e428b24b487b75a5f6ee0e1"
        "a56a26918fdf871b6cf09437e6588023a4ce423cf622621b2a7feee17612f704")
    assert Keypair(ss58_address=ed_alice, crypto_type=KeypairType.ED25519).verify(ed_payload, pinned)
    # sr25519 is randomised: sign, then verify against Alice's key.
    headers = {k.lower(): v for k, v in btauth.sign(alice, method="POST", target="/generate?stream=false",
                                                     body=GOLDEN_BODY, receiver=BOB, nonce=NONCE).items()}
    caller = btauth.verify(headers, GOLDEN_BODY, method="POST", target="/generate?stream=false", receivers=[BOB],
                           now_ns=NONCE, nonces=btauth.NonceStore())
    assert caller.hotkey == alice.ss58_address


def _signed_headers(**over):
    h = {k.lower(): v for k, v in btauth.sign(CUSTOMER, method="POST", target="/v1/x?a=1", body=b"{}",
                                               receiver=SERVICE.ss58_address, nonce=NONCE).items()}
    h.update(over)
    return {k: v for k, v in h.items() if v is not None}


@pytest.mark.parametrize("change, reason", [
    ({"x-bittensor-signature": None}, "malformed"),
    ({"x-bittensor-version": "2"}, "malformed"),
    ({"x-bittensor-hotkey": "not-an-address"}, "malformed"),
    ({"x-bittensor-nonce": "17e18"}, "malformed"),
    ({"x-bittensor-crypto": "ed25519"}, "unsupported_crypto"),
    ({"x-bittensor-crypto": "hashed"}, "unsupported_crypto"),
    ({"x-bittensor-receiver": None}, "wrong_receiver"),
    ({"x-bittensor-receiver": "-"}, "wrong_receiver"),
    ({"x-bittensor-receiver": OTHER.ss58_address}, "wrong_receiver"),
    ({"x-bittensor-hotkey": OTHER.ss58_address}, "bad_signature"),
])
def test_each_check_refuses(change, reason):
    with pytest.raises(btauth.BtAuthError) as exc:
        btauth.verify(_signed_headers(**change), b"{}", method="POST", target="/v1/x?a=1",
                      receivers=[SERVICE.ss58_address], now_ns=NONCE, nonces=btauth.NonceStore())
    assert exc.value.reason == reason


@pytest.mark.parametrize("method, target, body", [
    ("GET", "/v1/x?a=1", b"{}"), ("POST", "/v1/x?a=2", b"{}"), ("POST", "/v1/x", b"{}"),
    ("POST", "/v1/y?a=1", b"{}"), ("POST", "/v1/x?a=1", b"{ }"),
])
def test_signature_covers_method_path_query_and_body(method, target, body):
    with pytest.raises(btauth.BtAuthError) as exc:
        btauth.verify(_signed_headers(), body, method=method, target=target, receivers=[SERVICE.ss58_address],
                      now_ns=NONCE, nonces=btauth.NonceStore())
    assert exc.value.reason == "bad_signature"


def test_freshness_window_and_replay():
    ok = dict(method="POST", target="/v1/x?a=1", receivers=[SERVICE.ss58_address])
    for now in (NONCE - 2 * 10**9, NONCE + 10 * 10**9):  # the edges are accepted
        btauth.verify(_signed_headers(), b"{}", now_ns=now, nonces=btauth.NonceStore(), **ok)
    for now in (NONCE - 2 * 10**9 - 1, NONCE + 10 * 10**9 + 1):
        with pytest.raises(btauth.BtAuthError) as exc:
            btauth.verify(_signed_headers(), b"{}", now_ns=now, nonces=btauth.NonceStore(), **ok)
        assert exc.value.reason == "stale"
    store = btauth.NonceStore()
    btauth.verify(_signed_headers(), b"{}", now_ns=NONCE, nonces=store, **ok)
    with pytest.raises(btauth.BtAuthError) as exc:
        btauth.verify(_signed_headers(), b"{}", now_ns=NONCE + 1, nonces=store, **ok)
    assert exc.value.reason == "replayed"
    # A forged signature never reaches the store, so it can't burn a nonce.
    forged = _signed_headers(**{"x-bittensor-signature": "0x" + "00" * 64})
    store2 = btauth.NonceStore()
    with pytest.raises(btauth.BtAuthError):
        btauth.verify(forged, b"{}", now_ns=NONCE, nonces=store2, **ok)
    assert len(store2) == 0
    btauth.verify(_signed_headers(), b"{}", now_ns=NONCE, nonces=store2, **ok)


def test_nonce_store_forgets_after_the_window():
    store = btauth.NonceStore()
    for i in range(50):
        assert store.claim(b"k" * 32, NONCE + i, NONCE)
    assert len(store) == 50
    assert store.claim(b"k" * 32, NONCE + 100 * 10**9, NONCE + 100 * 10**9)
    assert len(store) == 1


# --- crediting ----------------------------------------------------------------------


def test_first_spend_opens_the_hotkeys_account_and_credits_it_once(env):
    assert credit(env, CUSTOMER.ss58_address, 5 * G) == "credited"
    user_id = owner_of(env, CUSTOMER.ss58_address)
    assert user_id and balance_cents(env, user_id) == 500
    with session_scope(env.sf) as s:
        user = s.get(UserORM, user_id)
        assert user.signup_method == "wallet" and user.email is None
        wallet = s.scalar(select(VerifiedWalletORM).where(VerifiedWalletORM.address == CUSTOMER.ss58_address))
        assert wallet.sign_in and wallet.chain == "bittensor"
        dep = s.scalar(select(CryptoDepositORM))
        assert (dep.currency, dep.status, dep.matched_by, dep.credited_cents, dep.amount_raw) == (
            "gamma", "credited", "gamma", 500, 5 * G)
        entry = s.scalar(select(LedgerEntryORM).where(LedgerEntryORM.user_id == user_id))
        assert entry.kind == "topup" and entry.deposit_ref == dep.deposit_ref
        assert entry.description.startswith("Crypto deposit GAMMA 5 spent by hotkey ")
    # Read again (a retried tick, a restart): nothing more.
    assert credit(env, CUSTOMER.ss58_address, 5 * G, block=101) == "seen"
    assert balance_cents(env, user_id) == 500
    # Grows again: only the growth.
    assert credit(env, CUSTOMER.ss58_address, 7 * G, block=102) == "credited"
    assert balance_cents(env, user_id) == 700
    with session_scope(env.sf) as s:
        assert s.scalar(select(func.count()).select_from(LedgerEntryORM)) == 2
        assert s.scalar(select(func.count()).select_from(GammaSpendORM)) == 2


def test_fractions_of_a_cent_carry_over(env):
    hk = CUSTOMER.ss58_address
    assert credit(env, hk, 123_456_789) == "credited"  # 12.3456789 cents
    user_id = owner_of(env, hk)
    assert balance_cents(env, user_id) == 12
    assert credit(env, hk, 123_456_789 + 4_000_000, block=101) == "logged"  # +0.4 cents: nothing yet
    assert balance_cents(env, user_id) == 12
    assert credit(env, hk, G, block=102) == "credited"  # exactly 1 gamma in all
    assert balance_cents(env, user_id) == 100


def test_a_verified_hotkey_pays_into_its_account(env):
    first = credit(env, OTHER.ss58_address, G)  # makes an account to link to
    assert first == "credited"
    email_user = owner_of(env, OTHER.ss58_address)
    with session_scope(env.sf) as s:
        s.add(VerifiedWalletORM(wallet_id="w-link", user_id=email_user, chain="bittensor",
                                address=CUSTOMER.ss58_address, sign_in=False))
    assert credit(env, CUSTOMER.ss58_address, 2 * G) == "credited"
    assert balance_cents(env, email_user) == 300
    with session_scope(env.sf) as s:
        assert s.scalar(select(func.count()).select_from(UserORM)) == 1  # no account made for it


def test_treasury_spends_are_buybacks_never_credit(env):
    assert credit(env, TREASURY.ss58_address, 250 * G) == "buyback"
    assert owner_of(env, TREASURY.ss58_address) is None
    with session_scope(env.sf) as s:
        assert s.scalar(select(func.count()).select_from(LedgerEntryORM)) == 0
        assert s.scalar(select(func.count()).select_from(CryptoDepositORM)) == 0
        spend = s.scalar(select(GammaSpendORM))
        assert spend.kind == "treasury" and spend.amount_raw == 250 * G
    totals = gamma_app.spent_totals(env.sf)
    assert totals["bought_back_gamma"] == "250" and totals["paid_by_customers_gamma"] == "0"


def test_spends_from_the_owner_coldkeys_deposits_are_buybacks(env, monkeypatch):
    # By default, SN110's owner coldkey: no setting needed for the first buyback.
    assert gamma.treasury_coldkeys() == {gamma.SN110_OWNER_COLDKEY}
    assert credit(env, OTHER.ss58_address, 40 * G, coldkey=gamma.SN110_OWNER_COLDKEY) == "buyback"
    assert owner_of(env, OTHER.ss58_address) is None
    monkeypatch.setenv("GREENCOMPUTE_GAMMA_TREASURY_COLDKEYS", TREASURY.ss58_address)
    assert credit(env, CUSTOMER.ss58_address, 2 * G, coldkey=TREASURY.ss58_address) == "buyback"
    # Another coldkey's deposit pays as usual.
    assert credit(env, THIRD.ss58_address, G, coldkey=OTHER.ss58_address) == "credited"
    with session_scope(env.sf) as s:
        assert s.scalar(select(func.count()).select_from(LedgerEntryORM)) == 1
    assert gamma_app.spent_totals(env.sf)["bought_back_gamma"] == "42"


def test_sanctioned_hotkey_or_coldkey_is_never_credited(env):
    assert credit(env, LISTED.ss58_address, 3 * G) == "blocked"
    assert owner_of(env, LISTED.ss58_address) is None
    assert env.alerts and "NOT credited" in env.alerts[-1]
    assert credit(env, CUSTOMER.ss58_address, 3 * G, coldkey=LISTED.ss58_address) == "blocked"
    with session_scope(env.sf) as s:
        assert s.scalar(select(func.count()).select_from(LedgerEntryORM)) == 0
        assert {d.status for d in s.scalars(select(CryptoDepositORM))} == {"blocked"}
    assert gamma_app.spent_totals(env.sf)["blocked_gamma"] == "6"


def test_a_new_account_waits_until_the_lists_have_loaded(env, monkeypatch):
    monkeypatch.setattr(sanctions, "lists_loaded", lambda sf: False)
    with pytest.raises(gamma_app.RetryLater):
        credit(env, CUSTOMER.ss58_address, G)
    with session_scope(env.sf) as s:
        assert s.scalar(select(func.count()).select_from(GammaSpentTotalORM)) == 0


def test_a_newer_subnet_counter_counts_from_zero(env):
    assert credit(env, CUSTOMER.ss58_address, 4 * G, counter=0) == "credited"
    user_id = owner_of(env, CUSTOMER.ss58_address)
    # Dissolved and registered again: the chain's totals start over.
    assert credit(env, CUSTOMER.ss58_address, 1 * G, counter=1, block=200) == "credited"
    assert balance_cents(env, user_id) == 500
    # A late read under the old counter changes nothing.
    assert credit(env, CUSTOMER.ss58_address, 9 * G, counter=0, block=201) == "seen"
    assert balance_cents(env, user_id) == 500


# --- signed requests through the app ------------------------------------------------


def test_signed_request_acts_as_the_hotkeys_account(env):
    credit(env, CUSTOMER.ss58_address, 5 * G)
    status, body = signed("GET", "/platform/secrets")
    assert status == 200, body
    # An OpenAI SDK's placeholder key is ignored when the request is signed.
    status, body = signed("GET", "/platform/secrets", authorization="Bearer sk-placeholder")
    assert status == 200, body
    status, body = signed("GET", "/platform/account/limits")
    assert status == 200, body
    with session_scope(env.sf) as s:
        keys = s.scalars(select(APIKeyORM)).all()
        assert len(keys) == 1
        assert keys[0].name == gamma.SIGNED_REQUEST_KEY_NAME
        assert sorted(keys[0].scopes) == ["inference", "read", "rentals"]
        assert keys[0].issued_by_wallet == f"bittensor:{CUSTOMER.ss58_address}#btauth"


def test_signed_post_body_reaches_the_route_unchanged(env):
    credit(env, CUSTOMER.ss58_address, 5 * G)
    status, body = signed("POST", "/platform/secrets", {"name": "HF_TOKEN", "value": "hf_abc"})
    assert status in (200, 201), body
    assert body["name"] == "HF_TOKEN"
    status, listed = signed("GET", "/platform/secrets")
    assert status == 200 and [s["name"] for s in listed] == ["HF_TOKEN"]


def test_query_string_is_part_of_the_signature(env):
    credit(env, CUSTOMER.ss58_address, 5 * G)
    raw_headers = btauth.sign(CUSTOMER, method="GET", target="/platform/secrets?limit=5", body=b"",
                              receiver=SERVICE.ss58_address)
    status, _ = call("GET", "/platform/secrets", headers=raw_headers, query=b"limit=5")
    assert status == 200
    raw_headers = btauth.sign(CUSTOMER, method="GET", target="/platform/secrets?limit=5", body=b"",
                              receiver=SERVICE.ss58_address)
    status, body = call("GET", "/platform/secrets", headers=raw_headers, query=b"limit=50")
    assert status == 401 and body["error"]["code"] == "signed_request_invalid"
    assert body["detail"]["reason"] == "bad_signature"


def test_a_bad_signature_is_refused_even_with_a_valid_key(env):
    credit(env, CUSTOMER.ss58_address, 5 * G)
    headers = btauth.sign(CUSTOMER, method="GET", target="/platform/secrets", body=b"",
                          receiver=OTHER.ss58_address)
    status, body = call("GET", "/platform/secrets", headers=headers)
    assert status == 401
    assert body["error"]["code"] == "signed_request_invalid" and body["detail"]["reason"] == "wrong_receiver"


def test_hotkey_with_no_account_is_told_how_to_pay(env):
    status, body = signed("GET", "/platform/secrets", key=OTHER)
    assert status == 402
    assert body["error"]["code"] == "gamma_payment_required"
    assert body["detail"]["pay"]["service_hotkey"] == SERVICE.ss58_address
    assert any("btcli gamma spend --netuid 110" in step for step in body["detail"]["pay"]["steps"])


def test_signed_requests_cannot_manage_keys_or_add_credit_or_be_admin(env):
    credit(env, CUSTOMER.ss58_address, 5 * G)
    status, body = signed("POST", "/platform/api-keys", {"name": "x"})
    assert status == 403 and body["detail"]["required_scope"] == "keys"
    status, body = signed("POST", "/platform/billing/topup/crypto", {"currency": "tao", "amount_usd": 10})
    assert status == 403 and body["detail"]["required_scope"] == "billing"
    status, _ = signed("GET", "/platform/admin/users")
    assert status in (403, 404)


def test_payment_only_link_cannot_sign_requests(env):
    credit(env, OTHER.ss58_address, G)
    user = owner_of(env, OTHER.ss58_address)
    with session_scope(env.sf) as s:
        s.add(VerifiedWalletORM(wallet_id="w-pay", user_id=user, chain="bittensor",
                                address=CUSTOMER.ss58_address, sign_in=False))
    status, body = signed("GET", "/platform/secrets")
    assert status == 403 and body["error"]["code"] == "wallet_sign_in_off"


def test_public_routes_ignore_the_signature(env):
    status, _ = signed("GET", "/gamma/services.json", key=OTHER)
    assert status == 200


def test_a_huge_signed_body_is_refused(env, monkeypatch):
    monkeypatch.setenv("GREENCOMPUTE_SIGNED_REQUEST_MAX_BODY_BYTES", "2048")
    raw = b"x" * 4096
    headers = btauth.sign(CUSTOMER, method="POST", target="/v1/chat/completions", body=raw,
                          receiver=SERVICE.ss58_address)
    status, body = call("POST", "/v1/chat/completions", raw=raw, headers=headers)
    assert status == 413 and body["error"]["code"] == "invalid_request"
    assert call.last_headers.get("access-control-allow-origin") == "*"


def test_revoked_key_comes_back_while_the_hotkey_signs_in(env):
    credit(env, CUSTOMER.ss58_address, 5 * G)
    assert signed("GET", "/platform/secrets")[0] == 200
    user = owner_of(env, CUSTOMER.ss58_address)
    wallet_auth.revoke_wallet_keys(env.sf, user, "bittensor", CUSTOMER.ss58_address)
    assert signed("GET", "/platform/secrets")[0] == 200
    with session_scope(env.sf) as s:
        s.execute(VerifiedWalletORM.__table__.update().values(sign_in=False))
    assert signed("GET", "/platform/secrets")[0] == 403


# --- the watcher --------------------------------------------------------------------


class FakeChain:
    def __init__(self, block=1000, counter=0):
        self.block, self.counter = block, counter
        self.totals: dict[str, dict[str, int]] = {}
        self.coldkeys: dict[str, str] = {}
        self.enabled, self.url = True, "https://api.green-compute.com/gamma/services.json"

    def get_chain_finalised_head(self):
        return f"0xhead{self.block}"

    def get_block_number(self, head):
        return int(head[6:])

    fail_deposits = False

    def query(self, module, name, params, block_hash=None):
        if name == "GammaDeposits" and self.fail_deposits:
            raise ConnectionError("rpc went away")
        v = {
            "RegisteredSubnetCounter": lambda: self.counter,
            "GammaDeposits": lambda: ({"coldkey": self.coldkeys[params[1]], "balance": 0, "daily_spend_cap": None}
                                      if params[1] in self.coldkeys else None),
            "SubnetGammaEnabled": lambda: self.enabled,
            "Services": lambda: {"url": self.url} if self.url else None,
            "SubnetOwner": lambda: gamma.SN110_OWNER_COLDKEY,
            "SubnetOwnerHotkey": lambda: SERVICE.ss58_address,
        }[name]()
        return types.SimpleNamespace(value=v)

    def query_map(self, module, name, params, block_hash=None, page_size=100):
        assert name == "GammaSpent"
        return [(types.SimpleNamespace(value=hk), types.SimpleNamespace(value=total))
                for hk, total in self.totals.get(params[1], {}).items()]

    # Blocks and their events: {block: [(hotkey, amount)]}, one extrinsic each.
    spends: dict = {}

    def get_block_hash(self, number):
        return f"0xblock{number}"

    def get_events(self, block_hash):
        number = int(block_hash[7:])
        return [types.SimpleNamespace(value={
            "phase": "ApplyExtrinsic", "extrinsic_idx": i,
            "event": {"module_id": "SubtensorModule", "event_id": "GammaSpent",
                      "attributes": {"netuid": 110, "hotkey": hk, "service": SERVICE.ss58_address, "amount": amount}},
        }) for i, (hk, amount) in enumerate(self.spends.get(number, []))]

    def get_block(self, block_hash=None):
        number = int(block_hash[7:])
        return {"extrinsics": [types.SimpleNamespace(extrinsic_hash=bytes([number % 256, i]) * 16)
                               for i in range(len(self.spends.get(number, [])))]}


def test_watcher_credits_growth_at_the_finalized_head(env):
    chain = FakeChain()
    chain.totals[SERVICE.ss58_address] = {CUSTOMER.ss58_address: 5 * G, TREASURY.ss58_address: 100 * G}
    assert gamma_watcher.scan_gamma(env.sf, env.billing, substrate=chain) == 1
    user = owner_of(env, CUSTOMER.ss58_address)
    assert balance_cents(env, user) == 500
    assert gamma_app.last_block(env.sf) == 1000
    # Nothing new: nothing credited.
    chain.block = 1001
    assert gamma_watcher.scan_gamma(env.sf, env.billing, substrate=chain) == 0
    chain.block, chain.totals[SERVICE.ss58_address][CUSTOMER.ss58_address] = 1002, 6 * G
    assert gamma_watcher.scan_gamma(env.sf, env.billing, substrate=chain) == 1
    assert balance_cents(env, user) == 600
    totals = gamma_app.spent_totals(env.sf)
    assert (totals["paid_by_customers_gamma"], totals["bought_back_gamma"], totals["total_spent_gamma"]) == (
        "6", "100", "106")


def test_watcher_skips_an_endpoint_behind_and_never_debits(env):
    chain = FakeChain(block=2000)
    chain.totals[SERVICE.ss58_address] = {CUSTOMER.ss58_address: 5 * G}
    gamma_watcher.scan_gamma(env.sf, env.billing, substrate=chain)
    user = owner_of(env, CUSTOMER.ss58_address)
    behind = FakeChain(block=1990)
    assert gamma_watcher.scan_gamma(env.sf, env.billing, substrate=behind) == 0
    assert not env.alerts
    gone = FakeChain(block=2010)  # the subnet's totals were cleared
    assert gamma_watcher.scan_gamma(env.sf, env.billing, substrate=gone) == 0
    assert balance_cents(env, user) == 500
    assert any("fell from 5 to 0 gamma" in a for a in env.alerts)


def test_watcher_screens_the_deposit_coldkey(env):
    chain = FakeChain()
    chain.totals[SERVICE.ss58_address] = {CUSTOMER.ss58_address: 2 * G}
    chain.coldkeys[CUSTOMER.ss58_address] = LISTED.ss58_address
    assert gamma_watcher.scan_gamma(env.sf, env.billing, substrate=chain) == 0
    assert owner_of(env, CUSTOMER.ss58_address) is None


def test_watcher_holds_its_cursor_while_a_spend_waits(env, monkeypatch):
    monkeypatch.setattr(sanctions, "lists_loaded", lambda sf: False)
    chain = FakeChain(block=3000)
    chain.totals[SERVICE.ss58_address] = {CUSTOMER.ss58_address: G}
    assert gamma_watcher.scan_gamma(env.sf, env.billing, substrate=chain) == 0
    assert gamma_app.last_block(env.sf) is None
    monkeypatch.setattr(sanctions, "lists_loaded", lambda sf: True)
    chain.block = 3001
    assert gamma_watcher.scan_gamma(env.sf, env.billing, substrate=chain) == 1
    assert gamma_app.last_block(env.sf) == 3001


def test_stalled_gamma_watcher_is_reported(env, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from greencompute_gateway.application import billing_monitor

    with session_scope(env.sf) as s:
        s.add(KeyValueStateORM(key=gamma_app.STATE_KEY, value="77",
                               updated_at=datetime.now(UTC) - timedelta(days=2)))
    with session_scope(env.sf) as s:
        found = billing_monitor.stalled_watchers(s, datetime.now(UTC))
    assert any(f.key == "watcher:gamma:77" for f in found)
    monkeypatch.setenv("GREENCOMPUTE_GAMMA_WATCHER_ENABLED", "false")
    with session_scope(env.sf) as s:
        assert not [f for f in billing_monitor.stalled_watchers(s, datetime.now(UTC)) if "gamma" in f.key]


def test_each_spend_gets_its_own_block_and_transaction(env):
    chain = FakeChain(block=5000)
    chain.totals[SERVICE.ss58_address] = {}
    gamma_watcher.scan_gamma(env.sf, env.billing, substrate=chain)  # the first read: nothing yet
    # Two spends by one hotkey before the next read, in blocks 5001 and 5003.
    chain.spends = {5001: [(OTHER.ss58_address, 9 * G), (CUSTOMER.ss58_address, 2 * G)],
                    5003: [(CUSTOMER.ss58_address, 3 * G)]}
    chain.block = 5003
    chain.totals[SERVICE.ss58_address] = {CUSTOMER.ss58_address: 5 * G, OTHER.ss58_address: 9 * G}
    assert gamma_watcher.scan_gamma(env.sf, env.billing, substrate=chain) == 3
    with session_scope(env.sf) as s:
        mine = s.scalars(select(GammaSpendORM).where(GammaSpendORM.hotkey == CUSTOMER.ss58_address)
                         .order_by(GammaSpendORM.block_number)).all()
        assert [(m.block_number, m.amount_raw, m.total_raw) for m in mine] == [(5001, 2 * G, 2 * G), (5003, 3 * G, 5 * G)]
        assert all(m.tx_hash and m.tx_hash.startswith("0x") for m in mine) and mine[0].tx_hash != mine[1].tx_hash
        # The deposit log carries the transaction too: what the "already
        # credited" check for a staff-confirmed invoice looks up.
        deposits = {d.tx_hash for d in s.scalars(select(CryptoDepositORM))}
        assert {m.tx_hash for m in mine} <= deposits
    assert balance_cents(env, owner_of(env, CUSTOMER.ss58_address)) == 500


def test_events_that_do_not_add_up_fall_back_to_the_total(env):
    chain = FakeChain(block=6000)
    chain.totals[SERVICE.ss58_address] = {}
    gamma_watcher.scan_gamma(env.sf, env.billing, substrate=chain)
    chain.spends = {6001: [(CUSTOMER.ss58_address, 1 * G)]}  # the chain says 4 were spent
    chain.block = 6001
    chain.totals[SERVICE.ss58_address] = {CUSTOMER.ss58_address: 4 * G}
    assert gamma_watcher.scan_gamma(env.sf, env.billing, substrate=chain) == 1
    with session_scope(env.sf) as s:
        spend = s.scalar(select(GammaSpendORM))
        assert (spend.amount_raw, spend.tx_hash, spend.block_number) == (4 * G, None, 6001)
    assert balance_cents(env, owner_of(env, CUSTOMER.ss58_address)) == 400


def test_a_failed_deposit_read_waits_rather_than_credit_a_buyback(env):
    chain = FakeChain(block=7000)
    chain.totals[SERVICE.ss58_address] = {OTHER.ss58_address: 1000 * G}
    chain.coldkeys[OTHER.ss58_address] = gamma.SN110_OWNER_COLDKEY
    chain.fail_deposits = True
    assert gamma_watcher.scan_gamma(env.sf, env.billing, substrate=chain) == 0
    assert owner_of(env, OTHER.ss58_address) is None and gamma_app.last_block(env.sf) is None
    chain.fail_deposits, chain.block = False, 7001
    assert gamma_watcher.scan_gamma(env.sf, env.billing, substrate=chain) == 0
    assert gamma_app.spent_totals(env.sf)["bought_back_gamma"] == "1000"
    assert owner_of(env, OTHER.ss58_address) is None


def test_the_service_hotkey_spending_at_itself_is_ours(env):
    assert credit(env, SERVICE.ss58_address, 7 * G) == "buyback"
    assert owner_of(env, SERVICE.ss58_address) is None


def test_a_counter_that_grows_without_a_reset_never_credits_again(env):
    assert credit(env, CUSTOMER.ss58_address, 50 * G, counter=0) == "credited"
    user = owner_of(env, CUSTOMER.ss58_address)
    # The counter is set for a subnet that was never dissolved: same totals.
    assert credit(env, CUSTOMER.ss58_address, 50 * G, counter=1, block=300) == "seen"
    assert credit(env, CUSTOMER.ss58_address, 52 * G, counter=1, block=301) == "credited"
    assert balance_cents(env, user) == 5200
    assert any("did not reset" in a for a in env.alerts)


def test_totals_keep_what_each_spend_was(env, monkeypatch):
    credit(env, CUSTOMER.ss58_address, 50 * G)
    monkeypatch.setenv("GREENCOMPUTE_GAMMA_TREASURY_HOTKEYS", CUSTOMER.ss58_address)
    assert credit(env, CUSTOMER.ss58_address, 51 * G, block=101) == "buyback"
    totals = gamma_app.spent_totals(env.sf)
    assert (totals["paid_by_customers_gamma"], totals["bought_back_gamma"]) == ("50", "1")
    assert gamma_app.balance(env.sf, CUSTOMER.ss58_address)["credited_usd"] == "50.00"


def test_no_key_a_user_made_can_stand_in_for_the_signed_request_key(env):
    from datetime import UTC, datetime, timedelta

    credit(env, CUSTOMER.ss58_address, 5 * G)
    user = owner_of(env, CUSTOMER.ss58_address)
    with session_scope(env.sf) as s:  # same name, the wallet's sign-in tag, full access, expired
        s.add(APIKeyORM(key_id="planted", user_id=user, name=gamma.SIGNED_REQUEST_KEY_NAME, admin=False, scopes=[],
                        secret="gk_planted", issued_by_wallet=f"bittensor:{CUSTOMER.ss58_address}",
                        expires_at=datetime.now(UTC) - timedelta(days=1)))
    status, body = signed("POST", "/platform/api-keys", {"name": "x"})
    assert status == 403 and body["detail"]["required_scope"] == "keys"
    assert signed("GET", "/platform/secrets")[0] == 200
    with session_scope(env.sf) as s:
        used = s.scalar(select(APIKeyORM).where(APIKeyORM.issued_by_wallet.like("%#btauth")))
        assert used.key_id != "planted"
        # Even if its row were widened, a signed request keeps its three scopes.
        used.scopes = []
    status, body = signed("POST", "/platform/api-keys", {"name": "x"})
    assert status == 403 and body["detail"]["required_scope"] == "keys"
    # Turning the hotkey's sign-in off (or unlinking it) deletes the key.
    wallet_auth.revoke_wallet_keys(env.sf, user, "bittensor", CUSTOMER.ss58_address)
    with session_scope(env.sf) as s:
        assert s.scalar(select(func.count()).select_from(APIKeyORM).where(
            APIKeyORM.issued_by_wallet.like("%#btauth"))) == 0


def test_an_api_key_still_works_next_to_a_stray_bittensor_header(env):
    credit(env, CUSTOMER.ss58_address, 5 * G)
    user = owner_of(env, CUSTOMER.ss58_address)
    with session_scope(env.sf) as s:
        s.add(APIKeyORM(key_id="k-plain", user_id=user, name="mine", admin=False, scopes=[], secret="gk_plain"))
    status, _ = call("GET", "/platform/secrets", headers={
        "Authorization": "Bearer gk_plain", "X-Bittensor-Hotkey": OTHER.ss58_address, "X-Bittensor-Version": "1"})
    assert status == 200


def test_a_wrapped_signature_is_refused():
    payload = btauth.build_payload(scheme="sr25519", method="POST", target="/v1/x?a=1", body=b"{}", nonce=NONCE,
                                   sender=CUSTOMER.ss58_address, receiver=SERVICE.ss58_address)
    wrapped = "0x" + CUSTOMER.sign(b"<Bytes>" + payload + b"</Bytes>").hex()
    with pytest.raises(btauth.BtAuthError) as exc:
        btauth.verify(_signed_headers(**{"x-bittensor-signature": wrapped}), b"{}", method="POST",
                      target="/v1/x?a=1", receivers=[SERVICE.ss58_address], now_ns=NONCE, nonces=btauth.NonceStore())
    assert exc.value.reason == "bad_signature"


def test_a_client_that_hangs_up_mid_body_gets_no_answer(env):
    headers = btauth.sign(CUSTOMER, method="POST", target="/platform/secrets", body=b"{}",
                          receiver=SERVICE.ss58_address)
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
        "path": "/platform/secrets", "raw_path": b"/platform/secrets", "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "client": ("203.0.113.9", 1), "server": ("api.test", 80),
    }
    messages = [{"type": "http.request", "body": b"{", "more_body": True}, {"type": "http.disconnect"}]
    sent = []

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    assert sent == []


def test_gamma_is_not_an_invoice_currency(env, monkeypatch):
    from greencompute_gateway.application import billing_service
    from greencompute_persistence import key_limits

    assert "gamma" not in billing_service.BONUS_RATES  # what /platform/billing/bonus-rates lists
    assert key_limits._bonus_rates()["gamma"] == 0.0 and gamma.bonus_pct() == 0.0
    # An all-separator setting falls back to the default rather than break the spec.
    monkeypatch.setenv("GREENCOMPUTE_GAMMA_SERVICE_HOTKEYS", " , ")
    assert gamma.service_hotkeys() == [gamma.SN110_OWNER_HOTKEY]


def test_watcher_backs_off_while_the_chain_cannot_be_read(env, monkeypatch):
    def refuse():
        raise ConnectionError("no endpoint")

    monkeypatch.setattr(gamma_watcher, "_connect", refuse)
    monkeypatch.setitem(gamma_watcher._failures, "n", 0)
    for _ in range(3):
        assert gamma_watcher.scan_gamma(env.sf, env.billing) == 0
    assert gamma_watcher._failures["n"] == 3
    chain = FakeChain()
    monkeypatch.setattr(gamma_watcher, "_connect", lambda: chain)
    gamma_watcher.scan_gamma(env.sf, env.billing)
    assert gamma_watcher._failures["n"] == 0


# --- the public documents -----------------------------------------------------------


def test_services_document_follows_the_standard(env):
    status, doc = call("GET", "/gamma/services.json")
    assert status == 200
    assert call.last_headers.get("access-control-allow-origin") == "*"
    assert doc["version"] == 1
    assert doc["publishers"] == [{"netuid": 110, "hotkey": SERVICE.ss58_address}]
    assert {"type": "gamma", "signature": "btauth/1", "balance": "https://api.green-compute.com/gamma/balance"} in doc[
        "payments"]
    assert any(s["type"] == "http" and s["spec"].endswith("/openapi.json") for s in doc["services"])
    assert call("GET", "/gamma/service.json")[1] == doc


def test_balance_endpoint(env):
    status, body = call("GET", "/gamma/balance", query=f"hotkey={OTHER.ss58_address}".encode())
    assert status == 200 and (body["credited_usd"], body["used_usd"], body["remaining_usd"]) == ("0.00", "0.00", "0.00")
    assert call.last_headers.get("access-control-allow-origin") == "*"
    credit(env, CUSTOMER.ss58_address, 5 * G)
    user = owner_of(env, CUSTOMER.ss58_address)
    with session_scope(env.sf) as s:
        s.get(UserORM, user).balance_credits = 320  # $1.80 used
    status, body = call("GET", "/gamma/balance", query=f"hotkey={CUSTOMER.ss58_address}".encode())
    assert (body["credited_usd"], body["used_usd"], body["remaining_usd"]) == ("5.00", "1.80", "3.20")
    assert call("GET", "/gamma/balance", query=b"hotkey=" + b"x" * 48)[0] == 400


def signed_balance(key, hotkey=None):
    query = f"hotkey={hotkey}" if hotkey else ""
    return signed("GET", "/gamma/balance", key=key, query=query)


def test_balance_never_shows_an_account_that_did_not_pay_in_gamma(env):
    # A wallet account (signed in with a coldkey, paid in TAO): no gamma here.
    user = wallet_auth.create_wallet_account(env.sf, "bittensor", OTHER.ss58_address)
    with session_scope(env.sf) as s:
        s.get(UserORM, user).balance_credits = 123_456
    status, body = call("GET", "/gamma/balance", query=f"hotkey={OTHER.ss58_address}".encode())
    assert status == 200
    assert (body["credited_usd"], body["used_usd"], body["remaining_usd"]) == ("0.00", "0.00", "0.00")
    # Its own signature shows its balance.
    status, body = signed_balance(OTHER)
    assert status == 200 and body["remaining_usd"] == "1234.56" and body["hotkey"] == OTHER.ss58_address
    # Someone else's signature doesn't.
    status, body = signed_balance(CUSTOMER, OTHER.ss58_address)
    assert status == 200 and body["remaining_usd"] == "0.00"


def test_balance_of_an_account_with_other_funding_needs_the_hotkeys_signature(env):
    credit(env, CUSTOMER.ss58_address, 5 * G)
    user = owner_of(env, CUSTOMER.ss58_address)
    with session_scope(env.sf) as s:  # a card payment too
        row = s.get(UserORM, user)
        row.balance_credits += 10_000
        s.add(LedgerEntryORM(entry_id="card-1", user_id=user, amount_cents=10_000, balance_after=10_500,
                             kind="topup", deposit_ref="stripe:cs_1", description="Stripe payment"))
    status, body = call("GET", "/gamma/balance", query=f"hotkey={CUSTOMER.ss58_address}".encode())
    assert status == 200
    assert (body["credited_usd"], body["used_usd"], body["remaining_usd"]) == ("5.00", None, None)
    assert "sign this request" in body["note"]
    status, body = signed_balance(CUSTOMER)
    assert (body["credited_usd"], body["remaining_usd"]) == ("5.00", "105.00")
    # A bad signature is told so, not served the public view.
    headers = btauth.sign(CUSTOMER, method="GET", target="/gamma/balance", body=b"", receiver=OTHER.ss58_address)
    status, body = call("GET", "/gamma/balance", headers=headers)
    assert status == 401 and body["error"]["code"] == "signed_request_invalid"


def test_balance_of_a_payment_only_hotkey_never_shows_the_account(env):
    credit(env, OTHER.ss58_address, G)
    user = owner_of(env, OTHER.ss58_address)
    with session_scope(env.sf) as s:
        s.add(VerifiedWalletORM(wallet_id="w-pay2", user_id=user, chain="bittensor",
                                address=CUSTOMER.ss58_address, sign_in=False))
    credit(env, CUSTOMER.ss58_address, 2 * G)
    with session_scope(env.sf) as s:
        s.get(UserORM, user).balance_credits = 150  # under what the hotkey paid
    for body in (gamma_app.balance(env.sf, CUSTOMER.ss58_address),
                 gamma_app.balance(env.sf, CUSTOMER.ss58_address, signed_by=CUSTOMER.ss58_address)):
        assert (body["credited_usd"], body["used_usd"], body["remaining_usd"]) == ("2.00", None, None)
    # The account's first hotkey no longer stands for the whole account either (two wallets now).
    body = gamma_app.balance(env.sf, OTHER.ss58_address)
    assert (body["credited_usd"], body["remaining_usd"]) == ("1.00", None)


def test_platform_gamma_and_openapi(env):
    status, body = call("GET", "/platform/gamma")
    assert status == 200 and body["service_hotkey"] == SERVICE.ss58_address and body["accepting"] is False
    chain = FakeChain()
    gamma_watcher._ticks["n"] = 0
    # A tick of the real loop records the chain's status (own connection).
    import greencompute_gateway.infrastructure.gamma_watcher as w

    w._conn["substrate"] = chain
    try:
        w.scan_gamma(env.sf, env.billing)
    finally:
        w._conn["substrate"] = None
    status, body = call("GET", "/platform/gamma")
    assert body["accepting"] is True and body["chain"]["service_url"].endswith("/gamma/services.json")
    app.openapi_schema = None
    spec = app.openapi()
    assert spec["components"]["securitySchemes"]["btauth"]["x-btauth"]["receivers"] == [SERVICE.ss58_address]
    assert {"btauth": []} in spec["paths"]["/v1/chat/completions"]["post"]["security"]
    assert {"btauth": []} not in spec["paths"]["/platform/api-keys"]["post"]["security"]
    assert spec["paths"]["/gamma/balance"]["get"]["security"] == []
    app.openapi_schema = None


# --- the weekly limit ---------------------------------------------------------------


def spend_rows(env, hotkey):
    with session_scope(env.sf) as s:
        return [(r.credited_cents, r.waiting_cents) for r in s.scalars(
            select(GammaSpendORM).where(GammaSpendORM.hotkey == hotkey).order_by(GammaSpendORM.total_raw)).all()]


def gamma_ledger(env, user_id):
    with session_scope(env.sf) as s:
        return [(e.deposit_ref.split(":")[-1], e.amount_cents) for e in s.scalars(
            select(LedgerEntryORM).where(LedgerEntryORM.user_id == user_id, LedgerEntryORM.kind == "topup")
            .order_by(LedgerEntryORM.created_at, LedgerEntryORM.amount_cents.desc())).all()]


def next_week(weeks=1):
    return gamma_app.week_start(datetime.now(UTC)) + timedelta(days=7 * weeks, hours=1)


def test_the_limit_is_1000_usd_a_week_until_staff_set_another(env):
    assert gamma_app.get_settings(env.sf) == {"weekly_limit_usd": 1000.0}
    status = gamma_app.limit_status(env.sf)
    assert (status["weekly_usd"], status["credited_this_week_usd"], status["remaining_this_week_usd"],
            status["waiting_usd"], status["credits_now"]) == ("1000.00", "0.00", "1000.00", "0.00", True)
    monday = gamma_app.week_start(datetime.now(UTC)).date()
    assert status["week_start"] == monday.isoformat() and monday.weekday() == 0
    assert status["week_end"] == (monday + timedelta(days=6)).isoformat()


def test_gamma_over_the_weekly_limit_waits_and_is_never_lost(env):
    # 1,500 gamma against a $1,000 week: $1,000 now, $500 waits.
    assert credit(env, CUSTOMER.ss58_address, 1500 * G) == "credited"
    user = owner_of(env, CUSTOMER.ss58_address)
    assert balance_cents(env, user) == 100_000
    assert spend_rows(env, CUSTOMER.ss58_address) == [(100_000, 50_000)]
    assert any("weekly limit is reached" in a for a in env.alerts)
    status = gamma_app.limit_status(env.sf, fresh=True)
    monday = gamma_app.week_start(datetime.now(UTC)).date()
    assert (status["credited_this_week_usd"], status["remaining_this_week_usd"], status["waiting_usd"],
            status["credits_now"]) == ("1000.00", "0.00", "500.00", False)
    # $500 is ahead: it takes next week's room first, and $500 of that week is left for new gamma.
    assert status["next_credit_from"] == (monday + timedelta(days=7)).isoformat()
    # Someone else spends 10 gamma the same week: all of it waits; their account is open, with nothing yet.
    assert credit(env, OTHER.ss58_address, 10 * G) == "waiting"
    other = owner_of(env, OTHER.ss58_address)
    assert balance_cents(env, other) == 0 and spend_rows(env, OTHER.ss58_address) == [(0, 1_000)]
    body = gamma_app.balance(env.sf, OTHER.ss58_address)
    assert (body["credited_usd"], body["waiting_usd"]) == ("0.00", "10.00")
    # Nothing moves while the week is full.
    assert gamma_app.release_waiting(env.sf, env.billing) == 0
    # The next week: both are credited, the older spend first.
    assert gamma_app.release_waiting(env.sf, env.billing, now=next_week()) == 2
    assert balance_cents(env, user) == 150_000 and balance_cents(env, other) == 1_000
    assert spend_rows(env, CUSTOMER.ss58_address) == [(150_000, 0)]
    assert spend_rows(env, OTHER.ss58_address) == [(1_000, 0)]
    # One ledger row per part, each with its own reference; one deposit-log row per spend, with its whole credit.
    parts = gamma_ledger(env, user)
    assert [cents for _, cents in parts] == [100_000, 50_000] and parts[1][0].endswith("#100000")
    assert [cents for _, cents in gamma_ledger(env, other)] == [1_000]
    with session_scope(env.sf) as s:
        deposits = {d.sender: d.credited_cents for d in s.scalars(select(CryptoDepositORM)).all()}
        total = s.scalar(select(GammaSpentTotalORM.credited_cents).where(
            GammaSpentTotalORM.hotkey == CUSTOMER.ss58_address))
    assert deposits == {CUSTOMER.ss58_address: 150_000, OTHER.ss58_address: 1_000} and total == 150_000
    # Released twice: nothing more.
    assert gamma_app.release_waiting(env.sf, env.billing, now=next_week()) == 0
    assert balance_cents(env, user) == 150_000
    # The account that is nothing but its hotkey's gamma still shows its balance.
    body = gamma_app.balance(env.sf, OTHER.ss58_address)
    assert (body["credited_usd"], body["remaining_usd"], body["waiting_usd"]) == ("10.00", "10.00", "0.00")


def test_a_long_queue_is_paid_week_by_week(env):
    credit(env, CUSTOMER.ss58_address, 3500 * G)     # $1,000 now, $2,500 waits
    user = owner_of(env, CUSTOMER.ss58_address)
    status = gamma_app.limit_status(env.sf, fresh=True)
    monday = gamma_app.week_start(datetime.now(UTC)).date()
    # Weeks 1 and 2 are full, week 3 has $500 left: new gamma is credited from then.
    assert status["next_credit_from"] == (monday + timedelta(days=21)).isoformat()
    for week, balance, waiting in ((1, 200_000, 150_000), (2, 300_000, 50_000), (3, 350_000, 0)):
        assert gamma_app.release_waiting(env.sf, env.billing, now=next_week(week)) == 1
        assert balance_cents(env, user) == balance
        assert spend_rows(env, CUSTOMER.ss58_address) == [(balance, waiting)]
    assert len({ref for ref, _ in gamma_ledger(env, user)}) == 4      # four parts, four references


def test_new_gamma_never_gets_ahead_of_gamma_that_is_waiting(env):
    credit(env, CUSTOMER.ss58_address, 1500 * G)     # $500 waits
    gamma_app.set_weekly_limit(env.sf, 1200)         # $200 of room opens, and $500 is waiting for it
    assert credit(env, OTHER.ss58_address, 100 * G) == "waiting"
    assert gamma_app.release_waiting(env.sf, env.billing) == 1
    assert spend_rows(env, CUSTOMER.ss58_address) == [(120_000, 30_000)]
    assert spend_rows(env, OTHER.ss58_address) == [(0, 10_000)]
    # With room for everyone waiting and more, a new spend is credited at once.
    gamma_app.set_weekly_limit(env.sf, 2000)
    assert credit(env, THIRD.ss58_address, 50 * G) == "credited"
    assert spend_rows(env, THIRD.ss58_address) == [(5_000, 0)]
    assert gamma_app.release_waiting(env.sf, env.billing) == 2
    assert gamma_app.limit_status(env.sf, fresh=True)["waiting_usd"] == "0.00"


def test_no_limit_and_a_limit_of_zero(env):
    assert gamma_app.set_weekly_limit(env.sf, None) == {"weekly_limit_usd": None}
    assert credit(env, CUSTOMER.ss58_address, 5000 * G) == "credited"
    assert spend_rows(env, CUSTOMER.ss58_address) == [(500_000, 0)]
    status = gamma_app.limit_status(env.sf, fresh=True)
    assert (status["weekly_usd"], status["remaining_this_week_usd"], status["credits_now"]) == (None, None, True)
    # Zero pauses crediting: every spend waits, and nothing says when.
    gamma_app.set_weekly_limit(env.sf, 0)
    assert credit(env, OTHER.ss58_address, 20 * G) == "waiting"
    status = gamma_app.limit_status(env.sf, fresh=True)
    assert (status["credits_now"], status["next_credit_from"], status["waiting_usd"]) == (False, None, "20.00")
    assert gamma_app.release_waiting(env.sf, env.billing, now=next_week()) == 0
    # Removing the limit credits everything that waited.
    gamma_app.set_weekly_limit(env.sf, None)
    assert gamma_app.release_waiting(env.sf, env.billing) == 1
    assert balance_cents(env, owner_of(env, OTHER.ss58_address)) == 2_000


def test_our_buybacks_do_not_use_the_limit(env):
    assert credit(env, TREASURY.ss58_address, 5000 * G) == "buyback"
    assert credit(env, CUSTOMER.ss58_address, 1000 * G) == "credited"
    assert spend_rows(env, CUSTOMER.ss58_address) == [(100_000, 0)]
    assert gamma_app.limit_status(env.sf, fresh=True)["remaining_this_week_usd"] == "0.00"


def test_only_this_weeks_gamma_credit_counts(env):
    credit(env, CUSTOMER.ss58_address, 900 * G)
    user = owner_of(env, CUSTOMER.ss58_address)
    with session_scope(env.sf) as s:
        # Last week's gamma, and a card payment this week: neither is this week's gamma credit.
        s.add(LedgerEntryORM(entry_id="old", user_id=user, amount_cents=80_000, balance_after=0, kind="topup",
                             deposit_ref="gamma:110:0:x:y:1", description="x",
                             created_at=gamma_app.week_start(datetime.now(UTC)) - timedelta(hours=1)))
        s.add(LedgerEntryORM(entry_id="card", user_id=user, amount_cents=70_000, balance_after=0, kind="topup",
                             deposit_ref="stripe:cs_9", description="x"))
    assert gamma_app.limit_status(env.sf, fresh=True)["remaining_this_week_usd"] == "100.00"
    assert credit(env, CUSTOMER.ss58_address, 1050 * G) == "credited"      # 150 more: 100 fits, 50 waits
    assert spend_rows(env, CUSTOMER.ss58_address) == [(90_000, 0), (10_000, 5_000)]


def test_waiting_gamma_of_a_deleted_account_is_dropped_and_staff_are_told(env):
    credit(env, CUSTOMER.ss58_address, 1500 * G)
    credit(env, OTHER.ss58_address, 10 * G)
    user = owner_of(env, CUSTOMER.ss58_address)
    with session_scope(env.sf) as s:      # deleted some other way than delete_account, which ends the wait itself
        s.get(UserORM, user).profile_metadata = {"deleted_at": "2026-11-27T00:00:00+00:00"}
    # It doesn't hold up the one behind it.
    assert gamma_app.release_waiting(env.sf, env.billing, now=next_week()) == 1
    assert spend_rows(env, CUSTOMER.ss58_address) == [(100_000, 0)]
    assert balance_cents(env, user) == 100_000
    assert spend_rows(env, OTHER.ss58_address) == [(1_000, 0)]
    assert any("has been deleted" in a and "$500.00" in a for a in env.alerts)


def test_an_account_merely_named_deleted_keeps_its_waiting_gamma(env):
    credit(env, CUSTOMER.ss58_address, 1500 * G)
    user = owner_of(env, CUSTOMER.ss58_address)
    with session_scope(env.sf) as s:
        s.get(UserORM, user).username = "deleted-scenes"      # a name anyone may choose
    assert gamma_app.release_waiting(env.sf, env.billing, now=next_week()) == 1
    assert balance_cents(env, user) == 150_000 and not env.alerts[1:]
    # And it can still sign its requests.
    assert signed("GET", "/platform/secrets")[0] == 200


def test_gamma_that_waited_is_screened_again_before_it_is_credited(env):
    """Listed after the spend, while its gamma waited: never credited."""
    credit(env, CUSTOMER.ss58_address, 1500 * G)                                # $500 waits
    credit(env, OTHER.ss58_address, 10 * G, coldkey=THIRD.ss58_address)         # all of it waits
    credit(env, TREASURY.ss58_address, G)
    key = Keypair.create_from_uri("//GammaFourth")
    credit(env, key.ss58_address, 20 * G)                                       # behind them, clean
    sanctions.store_list(env.sf, sanctions.OFAC, {
        ("bittensor", LISTED.ss58_address): "Listed Party",
        ("bittensor", CUSTOMER.ss58_address): "Now Listed",       # the hotkey itself
        ("bittensor", THIRD.ss58_address): "Now Listed Too",      # the other's deposit coldkey
    })
    assert gamma_app.release_waiting(env.sf, env.billing, now=next_week()) == 1
    customer, other, fourth = (owner_of(env, k.ss58_address) for k in (CUSTOMER, OTHER, key))
    assert (balance_cents(env, customer), balance_cents(env, other), balance_cents(env, fourth)) == (100_000, 0, 2_000)
    assert spend_rows(env, CUSTOMER.ss58_address) == [(100_000, 0)]
    assert spend_rows(env, OTHER.ss58_address) == [(0, 0)]
    blocked = [a for a in env.alerts if "NOT credited" in a]
    assert len(blocked) == 2 and any("$500.00" in a for a in blocked) and any(THIRD.ss58_address in a for a in blocked)
    with session_scope(env.sf) as s:
        deposits = {d.sender: (d.status, d.credited_cents) for d in s.scalars(select(CryptoDepositORM)).all()}
    # The spend nothing was credited of is logged as blocked; the part-credited one keeps its credited row.
    assert deposits[OTHER.ss58_address][0] == "blocked" and deposits[CUSTOMER.ss58_address] == ("credited", 100_000)
    # Nothing is left waiting to reserve the weeks ahead.
    assert gamma_app.limit_status(env.sf, fresh=True)["waiting_usd"] == "0.00"
    assert gamma_app.release_waiting(env.sf, env.billing, now=next_week(2)) == 0


def test_an_account_with_a_listed_wallet_gets_no_waiting_gamma(env):
    credit(env, CUSTOMER.ss58_address, 1500 * G)
    user = owner_of(env, CUSTOMER.ss58_address)
    with session_scope(env.sf) as s:      # the account also verified a wallet that is on the list
        s.add(VerifiedWalletORM(wallet_id="w-listed", user_id=user, chain="bittensor", address=LISTED.ss58_address,
                                sign_in=False))
    assert gamma_app.release_waiting(env.sf, env.billing, now=next_week()) == 0
    assert balance_cents(env, user) == 100_000 and spend_rows(env, CUSTOMER.ss58_address) == [(100_000, 0)]


def test_releasing_never_loads_the_account_before_its_lock(env, monkeypatch):
    """The account's row must first be read under its lock, in _credit: a
    row loaded earlier in the session is not refreshed by the locked read,
    and a charge or top-up committed meanwhile would be overwritten."""
    credit(env, CUSTOMER.ss58_address, 2500 * G)
    credit(env, CUSTOMER.ss58_address, 2600 * G)          # two waiting spends of one account
    user = owner_of(env, CUSTOMER.ss58_address)
    gamma_app.set_weekly_limit(env.sf, None)
    from sqlalchemy.orm import Session

    loads, real_get = [], Session.get

    def spy(self, entity, ident, **kwargs):
        if entity is UserORM:
            loads.append(bool(kwargs.get("with_for_update")))
        return real_get(self, entity, ident, **kwargs)

    monkeypatch.setattr(Session, "get", spy)
    assert gamma_app.release_waiting(env.sf, env.billing) == 2
    monkeypatch.undo()
    assert loads and all(loads), loads                    # every load of the account takes its lock
    assert balance_cents(env, user) == 260_000
    with session_scope(env.sf) as s:
        ledger_sum = s.scalar(select(func.sum(LedgerEntryORM.amount_cents)).where(LedgerEntryORM.user_id == user))
    assert ledger_sum == 260_000


def test_crediting_takes_its_turn_on_the_settings_row(env):
    with session_scope(env.sf) as s:
        assert s.get(KeyValueStateORM, gamma_app.SETTINGS_KEY) is None
    credit(env, CUSTOMER.ss58_address, 5 * G)
    with session_scope(env.sf) as s:
        assert s.get(KeyValueStateORM, gamma_app.SETTINGS_KEY).value == "{}"
    # The row made for the lock sets nothing: the default limit still applies, and staff can change it.
    assert gamma_app.get_settings(env.sf) == {"weekly_limit_usd": 1000.0}
    assert gamma_app.set_weekly_limit(env.sf, 50) == {"weekly_limit_usd": 50.0}
    assert credit(env, CUSTOMER.ss58_address, 100 * G) == "credited"
    assert spend_rows(env, CUSTOMER.ss58_address) == [(500, 0), (4_500, 5_000)]
    # A buyback takes no turn and makes no row of its own.
    assert credit(env, TREASURY.ss58_address, G) == "buyback"


def test_an_account_sees_its_own_gamma_credited_and_waiting(env):
    credit(env, CUSTOMER.ss58_address, 400 * G)
    credit(env, CUSTOMER.ss58_address, 1500 * G)          # 1,100 more: $600 fits, $500 waits
    credit(env, OTHER.ss58_address, 10 * G)               # someone else's, waiting too
    status, body = signed("GET", "/platform/billing/gamma")
    assert status == 200
    assert (body["credited_usd"], body["waiting_usd"]) == ("1000.00", "500.00") and "not lost" in body["waiting_note"]
    assert [(s["gamma"], s["credited_usd"], s["waiting_usd"]) for s in body["spends"]] == [
        ("1100", "600.00", "500.00"), ("400", "400.00", "0.00")]
    assert all(s["hotkey"] == CUSTOMER.ss58_address for s in body["spends"])
    assert body["limit"]["weekly_usd"] == "1000.00" and body["limit"]["credits_now"] is False
    assert call("GET", "/platform/billing/gamma")[0] == 401
    # With nothing waiting there is no note.
    gamma_app.set_weekly_limit(env.sf, None)
    gamma_app.release_waiting(env.sf, env.billing)
    assert signed("GET", "/platform/billing/gamma")[1]["waiting_note"] is None
    app.openapi_schema = None
    assert "/platform/billing/gamma" in app.openapi()["paths"]
    app.openapi_schema = None


def test_staff_are_told_when_waiting_gamma_keeps_failing_to_credit(env, monkeypatch):
    chain = FakeChain()
    monkeypatch.setattr(gamma_app, "release_waiting", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    gamma_watcher._release_failures["n"] = 0
    for _ in range(4):
        gamma_watcher.scan_gamma(env.sf, env.billing, substrate=chain)
    assert not [a for a in env.alerts if "failed" in a]
    for _ in range(3):
        gamma_watcher.scan_gamma(env.sf, env.billing, substrate=chain)
    assert len([a for a in env.alerts if "has failed 5 times in a row" in a]) == 1
    monkeypatch.undo()
    gamma_watcher.scan_gamma(env.sf, env.billing, substrate=chain)
    assert gamma_watcher._release_failures["n"] == 0


def test_deleting_an_account_says_what_waiting_gamma_is_lost_and_stops_it_waiting(env):
    from greencompute_gateway.application import account_deletion

    credit(env, CUSTOMER.ss58_address, 1500 * G)
    user = owner_of(env, CUSTOMER.ss58_address)
    with session_scope(env.sf) as s:
        s.get(UserORM, user).balance_credits = 250       # most of the $1,000 is used
    preview = account_deletion.deletion_preview(env.sf, user)
    assert (preview["balance_usd"], preview["gamma_waiting_usd"], preview["credit_lost_usd"]) == (2.5, 500.0, 502.5)
    out = account_deletion.delete_account(env.sf, user)
    assert (out["credit_forfeited_cents"], out["gamma_waiting_forfeited_cents"]) == (250, 50_000)
    # The spend keeps what it was worth and what was credited; nothing waits, nothing is credited later.
    assert spend_rows(env, CUSTOMER.ss58_address) == [(100_000, 0)]
    assert gamma_app.release_waiting(env.sf, env.billing, now=next_week()) == 0
    assert gamma_app.limit_status(env.sf, fresh=True)["waiting_usd"] == "0.00"
    assert not any("has been deleted" in a for a in env.alerts)


def test_the_watcher_credits_what_waited_before_anything_new(env):
    chain = FakeChain()
    chain.totals[SERVICE.ss58_address] = {CUSTOMER.ss58_address: 1500 * G}
    assert gamma_watcher.scan_gamma(env.sf, env.billing, substrate=chain) == 1
    user = owner_of(env, CUSTOMER.ss58_address)
    assert balance_cents(env, user) == 100_000
    # Staff raise the limit: the next tick credits the rest, with nothing new on chain.
    gamma_app.set_weekly_limit(env.sf, 5000)
    chain.block = 1001
    assert gamma_watcher.scan_gamma(env.sf, env.billing, substrate=chain) == 0
    assert balance_cents(env, user) == 150_000
    # A spend that waits doesn't hold the watcher's cursor: it was handled.
    assert gamma_app.last_block(env.sf) == 1001
    # A failing release never stops the tick.
    chain.block, chain.totals[SERVICE.ss58_address][CUSTOMER.ss58_address] = 1002, 1501 * G
    real = gamma_app.release_waiting
    gamma_app.release_waiting = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db hiccup"))
    try:
        assert gamma_watcher.scan_gamma(env.sf, env.billing, substrate=chain) == 1
    finally:
        gamma_app.release_waiting = real
    assert balance_cents(env, user) == 150_100


def test_limit_in_the_public_and_staff_views(env):
    with session_scope(env.sf) as s:
        s.add(APIKeyORM(key_id="admin", user_id=None, name="admin", admin=True, scopes=[], secret="gk_admin"))
    credit(env, CUSTOMER.ss58_address, 1500 * G)
    status, body = call("GET", "/platform/gamma")
    assert status == 200
    assert (body["limit"]["weekly_usd"], body["limit"]["waiting_usd"], body["limit"]["credits_now"]) == (
        "1000.00", "500.00", False)
    assert "not lost" in body["limit"]["note"]
    assert any("weekly" in step for step in body["steps"])
    status, body = call("GET", "/gamma/balance", query=f"hotkey={CUSTOMER.ss58_address}".encode())
    assert (body["credited_usd"], body["waiting_usd"]) == ("1000.00", "500.00")
    assert call("GET", "/gamma/services.json")[1]["limits"].endswith("/platform/gamma")
    # Staff only.
    admin = {"authorization": "Bearer gk_admin"}
    assert call("GET", "/platform/admin/gamma")[0] in (401, 403)
    assert call("PUT", "/platform/admin/gamma/settings", {"weekly_limit_usd": 1})[0] in (401, 403)
    status, body = call("GET", "/platform/admin/gamma", headers=admin)
    assert status == 200 and body["settings"] == {"weekly_limit_usd": 1000.0}
    assert [(w["hotkey"], w["credited_usd"], w["waiting_usd"], w["gamma"]) for w in body["waiting"]] == [
        (CUSTOMER.ss58_address, "1000.00", "500.00", "1500")]
    for bad in ({"weekly_limit_usd": -1}, {"weekly_limit_usd": "1000"}, {"weekly_limit_usd": True}, {},
                {"weekly_limit_usd": 5, "x": 1}):
        assert call("PUT", "/platform/admin/gamma/settings", bad, headers=admin)[0] == 400, bad
    status, body = call("PUT", "/platform/admin/gamma/settings", {"weekly_limit_usd": 2500.5}, headers=admin)
    assert status == 200 and body["settings"] == {"weekly_limit_usd": 2500.5}
    assert body["limit"]["weekly_usd"] == "2500.50"
    # The public view follows at once (its 10 s cache is dropped on a change).
    assert call("GET", "/platform/gamma")[1]["limit"]["weekly_usd"] == "2500.50"
    status, body = call("PUT", "/platform/admin/gamma/settings", {"weekly_limit_usd": None}, headers=admin)
    assert status == 200 and body["limit"]["weekly_usd"] is None
    # Neither route is in the public spec.
    app.openapi_schema = None
    assert not any(path.startswith("/platform/admin/gamma") for path in app.openapi()["paths"])
    app.openapi_schema = None

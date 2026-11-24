"""Paying with gamma, as Bittensor's Subnet services standard (and Lium, engy
and SayGM) do it: a hotkey spends SN110 gamma at our service hotkey, the
gamma watcher reads GammaSpent at the finalized head and credits the growth,
and the hotkey signs its requests (btauth/1).

Covered: the wire format against the standard's golden vectors and every
refusal; signed requests through the real ASGI app (body replayed, scopes,
placeholder bearer ignored, no account yet, payment-only link); crediting
exactly once, fractions of a cent, treasury buybacks, sanctions, a new
subnet counter; the watcher against a fake chain; the public documents."""
import asyncio
import hashlib
import json
import types

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
    wallet_auth._ALERTED.clear()
    gamma_watcher._ticks["n"] = 0
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
        assert keys[0].issued_by_wallet == f"bittensor:{CUSTOMER.ss58_address}"


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

    def query(self, module, name, params, block_hash=None):
        v = {
            "RegisteredSubnetCounter": lambda: self.counter,
            "GammaDeposits": lambda: ({"coldkey": self.coldkeys[params[1]], "balance": 0, "daily_spend_cap": None}
                                      if params[1] in self.coldkeys else None),
            "SubnetGammaEnabled": lambda: self.enabled,
            "Services": lambda: {"url": self.url} if self.url else None,
        }[name]()
        return types.SimpleNamespace(value=v)

    def query_map(self, module, name, params, block_hash=None, page_size=100):
        assert name == "GammaSpent"
        return [(types.SimpleNamespace(value=hk), types.SimpleNamespace(value=total))
                for hk, total in self.totals.get(params[1], {}).items()]


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

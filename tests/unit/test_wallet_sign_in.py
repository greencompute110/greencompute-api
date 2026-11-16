"""Signing in with a wallet: challenge -> signature -> API key, no email.

Requests go through the real ASGI app (middleware, error handlers), against
one SQLite database for every repository, so what is tested is what runs:
replay, cross-domain and expired messages, malleable or smart-wallet
signatures, address spellings, someone else's account, a wallet with two
owners, rate limits, and not saying whether a wallet has an account."""
import asyncio
import json
import types
from datetime import UTC, datetime, timedelta

import pytest
from eth_keys import keys
from eth_utils import keccak
from sqlalchemy import select
from substrateinterface import Keypair

from greencompute_gateway.application import sanctions, wallet_auth
from greencompute_gateway.application.billing_service import BillingService
from greencompute_gateway.domain import wallet_auth as msg
from greencompute_gateway.infrastructure.billing_repository import BillingRepository
from greencompute_gateway.infrastructure.repository import GatewayRepository
from greencompute_gateway.main import app
from greencompute_gateway.transport import routes, security
from greencompute_persistence import CredentialStore, FixedWindowRateLimiter, session_scope
from greencompute_persistence.orm import (
    AccountEventORM,
    APIKeyORM,
    UserORM,
    VerifiedWalletORM,
    WalletAuthChallengeORM,
)

PROVISION = "provision-secret-for-tests"
SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
EVM_KEY = keys.PrivateKey(bytes.fromhex("31" * 32))
EVM_ADDR = EVM_KEY.public_key.to_checksum_address()
LISTED_EVM = "0x" + "de" * 20


@pytest.fixture
def env(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'gc.sqlite'}"
    repo = GatewayRepository(database_url=url, bootstrap=True)
    billing = BillingService(BillingRepository(database_url=url))
    monkeypatch.setattr(routes.service, "repository", repo)
    monkeypatch.setattr(routes, "_get_billing", lambda: billing)
    monkeypatch.setattr(security, "credential_store", CredentialStore(session_factory=repo.session_factory))
    monkeypatch.setattr(security, "rate_limiter", FixedWindowRateLimiter())
    monkeypatch.setenv("GREENCOMPUTE_PROVISION_SECRET", PROVISION)
    monkeypatch.setenv("BILLING_DEPOSIT_USDC_BASE", "0x" + "ab" * 20)
    monkeypatch.setenv("BILLING_DEPOSIT_TAO", Keypair.create_from_uri("//Deposit").ss58_address)
    alerts, emails = [], []
    monkeypatch.setattr("greencompute_gateway.infrastructure.alerts.send_payment_alert",
                        lambda text, **kw: alerts.append(text))
    monkeypatch.setattr("greencompute_gateway.infrastructure.notify_website.notify_user",
                        lambda sf, uid, kind, fields, **kw: emails.append((uid, kind, fields)))
    wallet_auth._ALERTED.clear()
    sanctions._oracle_cache.clear()
    sanctions._oracle_failed_at.clear()
    # The lists have loaded once (one listed address), as they have in production.
    sanctions.store_list(repo.session_factory, sanctions.OFAC, {("evm", LISTED_EVM): "Listed Party"})
    return types.SimpleNamespace(sf=repo.session_factory, billing=billing, alerts=alerts, emails=emails)


def call(method, path, body=None, *, key=None, ip="203.0.113.7"):
    """One request through the ASGI app."""
    raw = json.dumps(body).encode() if body is not None else b""
    headers = [(b"content-type", b"application/json"), (b"host", b"api.test")]
    if key:
        headers.append((b"authorization", f"Bearer {key}".encode()))
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
        "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
        "headers": headers, "client": (ip, 1234), "server": ("api.test", 80),
    }
    sent = {"status": None, "body": b""}
    chunks = [{"type": "http.request", "body": raw, "more_body": False}]

    async def receive():
        return chunks.pop(0) if chunks else {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            sent["status"] = message["status"]
        elif message["type"] == "http.response.body":
            sent["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    return sent["status"], json.loads(sent["body"] or b"null")


def evm_sign(message: str, key=EVM_KEY, *, high_s=False) -> str:
    data = message.encode()
    digest = keccak(b"\x19Ethereum Signed Message:\n" + str(len(data)).encode() + data)
    sig = key.sign_msg_hash(digest)
    v, r, s = sig.v, sig.r, sig.s
    if high_s:  # the same signature's twin: (r, n - s) with the other parity
        s, v = SECP256K1_N - s, 1 - v
    return "0x" + r.to_bytes(32, "big").hex() + s.to_bytes(32, "big").hex() + bytes([v + 27]).hex()


def challenge(address, **extra):
    status, body = call("POST", "/platform/auth/wallet/challenge", {"address": address, **extra})
    assert status == 200, body
    return body


def verify(nonce, signature, **extra):
    return call("POST", "/platform/auth/wallet/verify", {"nonce": nonce, "signature": signature, **extra})


def sign_in_evm(key=EVM_KEY, **extra):
    ch = challenge(key.public_key.to_checksum_address())
    status, body = verify(ch["nonce"], evm_sign(ch["message"], key), **extra)
    assert status == 200, body
    return body


def user(sf, user_id) -> UserORM:
    with session_scope(sf) as s:
        row = s.get(UserORM, user_id)
        s.expunge(row)
        return row


# --- the happy paths ----------------------------------------------------------------


def test_an_evm_wallet_signs_in_and_gets_a_working_key_with_no_email(env):
    ch = challenge(EVM_ADDR.lower())
    lines = ch["message"].split("\n")
    # EIP-4361: domain line, the checksummed address, the fields wallets parse.
    assert lines[0] == "www.green-compute.com wants you to sign in with your Ethereum account:"
    assert lines[1] == EVM_ADDR
    assert f"Nonce: {ch['nonce']}" in lines and "URI: https://www.green-compute.com" in lines
    assert "Chain ID: 1" in lines and any(line.startswith("Expiration Time: ") for line in lines)
    assert ch["expires_in_seconds"] == 300

    status, out = verify(ch["nonce"], evm_sign(ch["message"]))
    assert status == 200, out
    assert out["api_key"].startswith("gk_") and out["account_created"] is True
    assert out["wallet"] == {"chain": "evm", "address": EVM_ADDR.lower()}
    assert out["account_limits"]["max_active_rentals"] == 2
    assert [d["currency"] for d in out["deposit_addresses"]] == ["usdc-base"]
    account = user(env.sf, out["user_id"])
    assert (account.email, account.signup_method) == (None, "wallet")

    status, balance = call("GET", "/platform/billing/balance", key=out["api_key"])
    assert (status, balance["balance_credits"]) == (200, 0)
    assert env.emails == []  # nobody to email


def test_a_bittensor_coldkey_signs_in_in_any_ss58_format_including_polkadot_js_wrapping(env):
    kp = Keypair.create_from_uri("//AgentColdkey")
    polkadot_format = Keypair(public_key=kp.public_key, ss58_format=0).ss58_address
    ch = challenge(polkadot_format)
    assert ch["chain"] == "bittensor" and ch["address"] == kp.ss58_address  # shown with prefix 42
    assert "\n" not in ch["message"] and ch["message"].startswith("www.green-compute.com wants you to sign in")
    wrapped = b"<Bytes>" + ch["message"].encode() + b"</Bytes>"  # what the extensions sign
    status, out = verify(ch["nonce"], "0x" + kp.sign(wrapped).hex())
    assert status == 200, out
    assert out["wallet"] == {"chain": "bittensor", "address": kp.ss58_address}
    # Signing in again, with the generic-format address, finds the same account.
    ch = challenge(kp.ss58_address)
    status, again = verify(ch["nonce"], "0x" + kp.sign(ch["message"]).hex())
    assert (status, again["user_id"], again["account_created"]) == (200, out["user_id"], False)


def test_scoped_key_with_spending_caps_from_day_one(env):
    out = sign_in_evm(scopes=["read", "inference"], daily_spend_limit_usd=5, max_active_rentals=1, key_name="bot")
    assert (out["scopes"], out["daily_spend_limit_usd"], out["max_active_rentals"], out["key_name"]) == (
        ["inference", "read"], 5.0, 1, "bot")
    status, body = call("POST", "/platform/deployments", {"workload_id": "w"}, key=out["api_key"])
    assert status == 403 and body["detail"]["required_scope"] == "rentals"
    status, body = verify("whatever", "0x00", scopes=["root"])
    assert status == 400 and "unknown scope" in body["error"]["message"].lower()


# --- replay -------------------------------------------------------------------------


def test_a_nonce_works_once(env):
    ch = challenge(EVM_ADDR)
    sig = evm_sign(ch["message"])
    assert verify(ch["nonce"], sig)[0] == 200
    status, body = verify(ch["nonce"], sig)
    assert status == 400 and body["error"]["code"] == "challenge_invalid"
    assert "already used" in body["error"]["message"] and "request a new one" in body["error"]["message"]


def test_a_failed_attempt_spends_the_nonce(env):
    ch = challenge(EVM_ADDR)
    other = keys.PrivateKey(bytes.fromhex("42" * 32))
    status, body = verify(ch["nonce"], evm_sign(ch["message"], other))
    assert status == 401 and body["error"]["code"] == "signature_invalid"
    assert verify(ch["nonce"], evm_sign(ch["message"]))[1]["error"]["code"] == "challenge_invalid"


def test_an_expired_challenge_is_refused(env):
    ch = challenge(EVM_ADDR)
    with session_scope(env.sf) as s:
        s.get(WalletAuthChallengeORM, ch["nonce"]).expires_at = datetime.now(UTC) - timedelta(seconds=1)
    status, body = verify(ch["nonce"], evm_sign(ch["message"]))
    assert status == 400 and body["error"]["code"] == "challenge_invalid" and "expired" in body["error"]["message"]


def test_unknown_or_malformed_nonce(env):
    for nonce in ("0" * 32, "../../etc", ""):
        status, body = verify(nonce, "0x" + "00" * 65)
        assert status in (400, 422) and body["error"]["code"] in ("challenge_invalid", "validation_error")


def test_a_signature_for_another_domain_does_not_verify(env):
    """A phishing site could get our nonce signed inside its own message;
    we check the signature against the text we issued, so it fails."""
    ch = challenge(EVM_ADDR)
    forged = ch["message"].replace("www.green-compute.com", "green-compute.evil.example")
    status, body = verify(ch["nonce"], evm_sign(forged))
    assert status == 401 and body["error"]["code"] == "signature_invalid"


def test_a_challenge_for_one_wallet_cant_sign_in_another(env):
    """The nonce binds the address: a valid signature by wallet B over A's
    challenge is not a sign-in for either."""
    victim = keys.PrivateKey(bytes.fromhex("51" * 32))
    victim_account = sign_in_evm(victim)["user_id"]
    ch = challenge(victim.public_key.to_checksum_address())
    status, _ = verify(ch["nonce"], evm_sign(ch["message"], EVM_KEY))
    assert status == 401
    mine = sign_in_evm(EVM_KEY)
    assert mine["user_id"] != victim_account


# --- signatures -----------------------------------------------------------------------


def test_the_high_s_twin_of_a_valid_signature_is_refused(env):
    ch = challenge(EVM_ADDR)
    status, body = verify(ch["nonce"], evm_sign(ch["message"], high_s=True))
    assert status == 401 and body["error"]["code"] == "signature_invalid"


def test_a_smart_wallet_signature_says_what_to_do(env):
    ch = challenge(EVM_ADDR)
    status, body = verify(ch["nonce"], "0x" + "ab" * 300)
    assert status == 400 and body["error"]["code"] == "signature_invalid"
    assert "smart-contract wallet" in body["error"]["message"]


def test_a_bittensor_signature_by_another_key_is_refused(env):
    kp, other = Keypair.create_from_uri("//A1"), Keypair.create_from_uri("//B1")
    ch = challenge(kp.ss58_address)
    status, body = verify(ch["nonce"], "0x" + other.sign(ch["message"]).hex())
    assert status == 401 and "coldkey" in body["error"]["message"]


# --- addresses --------------------------------------------------------------------------


def test_lowercase_and_checksummed_addresses_are_one_account_and_a_bad_checksum_is_refused(env):
    first = sign_in_evm()
    ch = challenge(EVM_ADDR.lower())
    assert verify(ch["nonce"], evm_sign(ch["message"]))[1]["user_id"] == first["user_id"]
    broken = EVM_ADDR[:2] + EVM_ADDR[2:].swapcase()
    if broken[2:] not in (broken[2:].lower(), broken[2:].upper()):
        status, body = call("POST", "/platform/auth/wallet/challenge", {"address": broken})
        assert status == 400 and "checksum" in body["error"]["message"]
    for bad in ("0x1234", "5notanaddress", "", "0x" + "zz" * 20):
        status, _ = call("POST", "/platform/auth/wallet/challenge", {"address": bad})
        assert status in (400, 422)
    status, body = call("POST", "/platform/auth/wallet/challenge", {"address": EVM_ADDR, "chain_id": 137})
    assert status == 400 and "8453" in body["error"]["message"]
    assert challenge(EVM_ADDR, chain_id=8453)["message"].count("Chain ID: 8453") == 1


# --- keys: rotation, revocation -------------------------------------------------------------


def test_signing_in_again_revokes_only_keys_wallet_sign_in_issued(env):
    first = sign_in_evm()
    # A key made WITH the wallet's key is the wallet's too (revoked with it);
    # the website's own key never is.
    status, own = call("POST", "/platform/api-keys", {"name": "mine"}, key=first["api_key"])
    assert status == 200
    status, site = call("POST", "/platform/api-keys", {"name": "default", "user_id": first["user_id"]}, key=PROVISION)
    assert status == 200

    second = sign_in_evm()
    assert set(second["revoked_key_ids"]) == {first["key_id"], own["key_id"]}
    for gone in (first["api_key"], own["secret"]):
        assert call("GET", "/platform/billing/balance", key=gone)[0] == 401
    for working in (second["api_key"], site["secret"]):
        assert call("GET", "/platform/billing/balance", key=working)[0] == 200

    third = sign_in_evm(keep_existing_keys=True)
    assert third["revoked_key_ids"] == []
    assert call("GET", "/platform/billing/balance", key=second["api_key"])[0] == 200


def test_a_revoke_challenge_revokes_and_issues_nothing(env):
    out = sign_in_evm()
    ch = challenge(EVM_ADDR, action="revoke_keys")
    assert "Revoke every Green Compute API key" in ch["message"]
    status, body = verify(ch["nonce"], evm_sign(ch["message"]))
    assert (status, body["api_key"], body["revoked_key_ids"]) == (200, None, [out["key_id"]])
    assert call("GET", "/platform/billing/balance", key=out["api_key"])[0] == 401


def test_revoking_for_a_wallet_with_no_account_creates_none(env):
    stranger = keys.PrivateKey(bytes.fromhex("61" * 32))
    ch = challenge(stranger.public_key.to_checksum_address(), action="revoke_keys")
    status, body = verify(ch["nonce"], evm_sign(ch["message"], stranger))
    assert (status, body["user_id"], body["revoked_key_ids"]) == (200, None, [])
    with session_scope(env.sf) as s:
        assert s.scalar(select(UserORM).where(UserORM.signup_method == "wallet")) is None


# --- whose account ---------------------------------------------------------------------------


def _email_account(env, user_id="email-user", email="ada@example.com"):
    with session_scope(env.sf) as s:
        s.add(UserORM(user_id=user_id, username="ada", email=email, balance_credits=0, profile_metadata={}))
    return user_id


def _approve_sign_in(approver, wallet) -> dict:
    """The PATCH body that turns on `wallet`'s sign-in, signed by `approver`
    (an EVM key that signs in to the account already)."""
    subject = f"{wallet['chain']}:{wallet['address']}"
    ch = challenge(approver.public_key.to_checksum_address(), action="approve_sign_in", subject=subject)
    return {"sign_in": True, "nonce": ch["nonce"], "signature": evm_sign(ch["message"], approver)}


def _link_for_sign_in(env, user_id, address):
    """A wallet the account linked to pay in, with signing in turned on."""
    wallet, _ = env.billing.repo.add_verified_wallet(user_id, "evm", address)
    env.billing.repo.set_wallet_sign_in(user_id, wallet["wallet_id"], True)
    return wallet


def test_a_wallet_verified_on_an_email_account_signs_in_to_that_account_and_the_owner_is_told(env):
    uid = _email_account(env)
    _link_for_sign_in(env, uid, EVM_ADDR.lower())
    out = sign_in_evm()
    assert (out["user_id"], out["account_created"], out["has_email"]) == (uid, False, True)
    assert out["account_limits"] is None  # not a new wallet account
    assert [(e[0], e[1]) for e in env.emails] == [(uid, "wallet_sign_in")]
    assert env.emails[0][2]["address"] == EVM_ADDR.lower()


def test_a_wallet_has_one_owner(env):
    """A wallet account's wallet can't be linked to another account, and
    the link flow's own challenge can't be replayed as a sign-in."""
    out = sign_in_evm()
    other = _email_account(env, "u2", "bob@example.com")
    ch = env.billing.wallet_challenge(other, "evm", EVM_ADDR)
    from greencompute_gateway.infrastructure.billing_repository import WalletLinkedElsewhere

    with pytest.raises(WalletLinkedElsewhere):
        env.billing.link_wallet(other, "evm", EVM_ADDR, ch["issued_at"], evm_sign(ch["message"]))
    with session_scope(env.sf) as s:
        owners = s.scalars(select(VerifiedWalletORM.user_id).where(VerifiedWalletORM.address == EVM_ADDR.lower())).all()
    assert owners == [out["user_id"]]


def test_two_first_sign_ins_racing_make_one_account(env):
    first = wallet_auth._create_account(env.sf, "evm", EVM_ADDR.lower())
    assert wallet_auth._create_account(env.sf, "evm", EVM_ADDR.lower()) is None  # the unique index decides
    assert wallet_auth._account_for(env.sf, "evm", EVM_ADDR.lower()) == (first, False)


def test_challenges_say_nothing_about_whether_a_wallet_has_an_account(env):
    newcomer = keys.PrivateKey(bytes.fromhex("71" * 32)).public_key.to_checksum_address()
    sign_in_evm()
    a, b = challenge(EVM_ADDR), challenge(newcomer)
    assert set(a) == set(b)
    for field in ("nonce", "message", "address", "issued_at", "expires_at"):
        a.pop(field), b.pop(field)
    assert a == b


def test_rate_limits_per_ip_and_none_per_wallet(env, monkeypatch):
    monkeypatch.setattr(routes, "WALLET_AUTH_PER_IP", 3)
    for _ in range(3):
        challenge(EVM_ADDR)
    status, body = call("POST", "/platform/auth/wallet/challenge", {"address": EVM_ADDR})
    assert status == 429 and body["error"]["code"] == "rate_limited"
    # Another address gets its own allowance; the website server is limited per visitor itself.
    assert call("POST", "/platform/auth/wallet/challenge", {"address": EVM_ADDR}, ip="198.51.100.1")[0] == 200
    for _ in range(3):
        assert call("POST", "/platform/auth/wallet/verify", {"nonce": "x" * 32, "signature": "0x00"},
                    ip="198.51.100.2")[0] == 400
    assert call("POST", "/platform/auth/wallet/verify", {"nonce": "x" * 32, "signature": "0x00"},
                ip="198.51.100.2")[0] == 429
    # Strangers asking for one wallet's challenges from many addresses can't
    # lock its owner out: there is no per-wallet limit to use up.
    target = keys.PrivateKey(bytes.fromhex("81" * 32))
    for i in range(10):
        call("POST", "/platform/auth/wallet/challenge", {"address": target.public_key.to_checksum_address()},
             ip=f"192.0.2.{i}")
    monkeypatch.setattr(security, "rate_limiter", FixedWindowRateLimiter())  # a new minute for this IP
    assert sign_in_evm(target)["account_created"] is True


# --- sanctions at sign-in ------------------------------------------------------------------------


def test_a_listed_wallet_gets_the_neutral_message_and_no_account(env):
    key = keys.PrivateKey(bytes.fromhex("91" * 32))
    addr = key.public_key.to_checksum_address().lower()
    sanctions.store_list(env.sf, sanctions.OFAC, {("evm", addr): "Listed"})
    ch = challenge(addr)
    status, body = verify(ch["nonce"], evm_sign(ch["message"], key))
    assert status == 403 and body["error"]["code"] == "wallet_blocked"
    assert body["error"]["message"] == "This wallet can't be used with Green Compute. Contact support@green-compute.com."
    assert "ofac" not in json.dumps(body).lower() and "sanction" not in json.dumps(body).lower()
    assert wallet_auth.wallet_owner(env.sf, "evm", addr) is None
    assert len(env.alerts) == 1 and addr in env.alerts[0]


def test_the_oracle_blocks_an_evm_wallet_the_lists_dont_name(env, monkeypatch):
    monkeypatch.setenv("GREENCOMPUTE_SANCTIONS_ORACLE_ENABLED", "true")
    sanctions._oracle_cache.clear()
    monkeypatch.setattr(sanctions.sanctions_lists, "oracle_is_sanctioned", lambda a: a == EVM_ADDR.lower())
    ch = challenge(EVM_ADDR)
    assert verify(ch["nonce"], evm_sign(ch["message"]))[1]["error"]["code"] == "wallet_blocked"


def test_new_sign_ups_fail_closed_until_the_lists_have_loaded_but_existing_accounts_sign_in(env):
    existing = sign_in_evm()
    with session_scope(env.sf) as s:
        from greencompute_persistence.orm import KeyValueStateORM

        s.delete(s.get(KeyValueStateORM, "sanctions:ofac_sdn:loaded_at"))
    newcomer = keys.PrivateKey(bytes.fromhex("a1" * 32))
    ch = challenge(newcomer.public_key.to_checksum_address())
    status, body = verify(ch["nonce"], evm_sign(ch["message"], newcomer))
    assert status == 503 and body["error"]["code"] == "screening_unavailable"
    assert body["error"]["retryable"] is False  # a new challenge is needed
    assert sign_in_evm()["user_id"] == existing["user_id"]


# --- the website's sign-in --------------------------------------------------------------------------


def test_the_website_session_needs_the_provision_secret_and_leaves_keys_alone(env):
    agent = sign_in_evm()
    ch = challenge(EVM_ADDR)
    body = {"nonce": ch["nonce"], "signature": evm_sign(ch["message"])}
    assert call("POST", "/platform/auth/wallet/website-session", body, key=agent["api_key"])[0] in (401, 403)
    status, out = call("POST", "/platform/auth/wallet/website-session", body, key=PROVISION)
    assert status == 200, out
    assert (out["user_id"], out["address"], out["account_created"]) == (agent["user_id"], EVM_ADDR.lower(), False)
    # A key for this website session alone, tagged apart from the agent's.
    assert out["api_key"].startswith("gk_") and out["api_key"] != agent["api_key"]
    with session_scope(env.sf) as s:
        assert s.get(APIKeyORM, out["key_id"]).issued_by_wallet == f"evm:{EVM_ADDR.lower()}#web"
    assert call("GET", "/platform/billing/balance", key=agent["api_key"])[0] == 200  # nothing revoked


def test_the_website_signs_up_a_new_wallet(env):
    kp = Keypair.create_from_uri("//WebsiteVisitor")
    ch = challenge(kp.ss58_address)
    status, out = call("POST", "/platform/auth/wallet/website-session",
                       {"nonce": ch["nonce"], "signature": "0x" + kp.sign(ch["message"]).hex()}, key=PROVISION)
    assert status == 200 and out["account_created"] is True and out["email"] is None
    assert out["display_name"].startswith(kp.ss58_address[:6])


def test_a_confirmed_email_ends_the_new_account_limits(env):
    out = sign_in_evm()
    status, body = call("POST", "/platform/account/verified-email",
                        {"user_id": out["user_id"], "email": "New@Example.com"}, key=PROVISION)
    assert status == 200 and body["email"] == "new@example.com"
    assert user(env.sf, out["user_id"]).email_verified_at is not None
    assert sign_in_evm()["account_limits"] is None
    _email_account(env, "u3", "taken@example.com")
    assert call("POST", "/platform/account/verified-email",
                {"user_id": out["user_id"], "email": "taken@example.com"}, key=PROVISION)[0] == 409
    assert call("POST", "/platform/account/verified-email",
                {"user_id": out["user_id"], "email": "x@y.z"}, key=out["api_key"])[0] in (401, 403)


# --- paying from the wallet ----------------------------------------------------------------------------


def test_a_deposit_from_the_wallet_is_credited_with_no_invoice_and_an_event(env):
    out = sign_in_evm()
    result = env.billing.resolve_deposit({
        "deposit_ref": "usdc-base:0xpay:0", "currency": "usdc-base", "tx_hash": "0xpay",
        "sender": EVM_ADDR.lower(), "deposit_address": "0x" + "ab" * 20, "amount_raw": 12_500_000,
        "decimals": 6, "block_number": 1, "usd_price": 1.0,
    })
    assert result == "wallet"
    assert env.billing.repo.get_balance(out["user_id"]) == 1250
    with session_scope(env.sf) as s:
        event = s.scalar(select(AccountEventORM).where(AccountEventORM.user_id == out["user_id"]))
        assert event.type == "payment.credited"
        assert (event.data["amount_usd"], event.data["balance_usd"], event.data["sender"]) == (
            12.5, 12.5, EVM_ADDR.lower())


def test_payments_sent_before_signing_up_are_claimed_at_sign_up(env):
    kp = Keypair.create_from_uri("//EarlyPayer")
    env.billing.resolve_deposit({
        "deposit_ref": "tao:9:1", "currency": "tao", "tx_hash": "0xearly", "sender": kp.ss58_address,
        "deposit_address": Keypair.create_from_uri("//Deposit").ss58_address, "amount_raw": 10**8,
        "decimals": 9, "block_number": 9, "usd_price": 300.0,
    })
    ch = challenge(kp.ss58_address)
    status, out = verify(ch["nonce"], "0x" + kp.sign(ch["message"]).hex())
    assert status == 200 and [c["deposit_ref"] for c in out["credited"]] == ["tao:9:1"]
    assert env.billing.repo.get_balance(out["user_id"]) == 3000


def test_the_only_wallet_of_a_wallet_account_cant_be_unlinked_and_unlinking_revokes_its_keys(env):
    out = sign_in_evm()
    [wallet] = env.billing.repo.list_verified_wallets(out["user_id"])
    status, body = call("DELETE", f"/platform/billing/wallets/{wallet['wallet_id']}", key=out["api_key"])
    assert status == 409 and "signs in" in body["error"]["message"]
    # An email account's wallet: unlinking it also ends the keys it signed in for.
    uid = _email_account(env)
    other = keys.PrivateKey(bytes.fromhex("b1" * 32))
    _link_for_sign_in(env, uid, other.public_key.to_checksum_address().lower())
    issued = sign_in_evm(other)
    [w] = env.billing.repo.list_verified_wallets(uid)
    status, body = call("DELETE", f"/platform/billing/wallets/{w['wallet_id']}", key=issued["api_key"])
    assert status == 200 and body["revoked_key_ids"] == [issued["key_id"]]
    assert call("GET", "/platform/billing/balance", key=issued["api_key"])[0] == 401


def test_wallet_accounts_get_no_payment_emails():
    """notify_user skips accounts without an email: low credit, stopped
    rentals, payments and the rest all go through it."""
    from greencompute_gateway.infrastructure import notify_website

    repo = GatewayRepository(database_url="sqlite:///:memory:", bootstrap=True)
    uid = wallet_auth._create_account(repo.session_factory, "evm", EVM_ADDR.lower())
    assert notify_website._recipient(repo.session_factory, uid) is None


def test_issued_keys_are_tagged_with_their_wallet(env):
    out = sign_in_evm()
    with session_scope(env.sf) as s:
        row = s.get(APIKeyORM, out["key_id"])
        assert row.issued_by_wallet == f"evm:{EVM_ADDR.lower()}" and row.admin is False


def test_the_message_names_the_configured_domain(monkeypatch):
    monkeypatch.setenv("GREENCOMPUTE_WALLET_AUTH_DOMAIN", "staging.green-compute.com")
    now = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
    text = msg.sign_in_message(chain="bittensor", display_address="5Abc", action="sign_in", nonce="n" * 32,
                               issued_at=now, expires_at=now + timedelta(minutes=5))
    assert text.startswith("staging.green-compute.com wants you to sign in")
    assert "URI: https://staging.green-compute.com." in text and "Expiration Time: 2026-11-16T12:05:00Z." in text


def test_the_two_calls_are_in_the_openapi_spec_and_the_website_ones_are_not():
    spec = app.openapi()
    paths, schemas = spec["paths"], spec["components"]["schemas"]
    for path, model in (("/platform/auth/wallet/challenge", "WalletChallenge"),
                        ("/platform/auth/wallet/verify", "WalletSignIn")):
        op = paths[path]["post"]
        assert op["responses"]["200"]["content"]["application/json"]["schema"] == {"$ref": f"#/components/schemas/{model}"}
        assert "requestBody" in op and op["description"]
    assert {"scopes", "daily_spend_limit_usd", "max_active_rentals", "keep_existing_keys"} <= set(
        schemas["WalletVerifyRequest"]["properties"])
    assert "/platform/auth/wallet/website-session" not in paths
    assert "/platform/account/verified-email" not in paths


# --- what a free wallet account must not be able to do ------------------------------------


def test_a_wallet_account_cant_claim_someone_elses_email(env):
    """Squatting: a wallet account setting victim@ as its email would be handed
    the victim's later website sign-up (register_user joins by email)."""
    out = sign_in_evm()
    status, body = call("PATCH", f"/platform/users/{out['user_id']}", {"email": "victim@example.com"}, key=out["api_key"])
    assert status == 403 and "Settings" in body["error"]["message"]
    status, _ = call("PATCH", f"/platform/users/{out['user_id']}", {"display_name": "Agent 7"}, key=out["api_key"])
    assert status == 200
    # Even with the address set some other way, sign-up never joins an unconfirmed wallet account.
    with session_scope(env.sf) as s:
        s.get(UserORM, out["user_id"]).email = "victim@example.com"
    status, reg = call("POST", "/platform/register", {"username": "victim", "email": "victim@example.com"}, key=PROVISION)
    assert status == 200 and reg["user_id"] != out["user_id"]
    # A confirmed one is the account's own: sign-up joins it.
    call("POST", "/platform/account/verified-email", {"user_id": out["user_id"], "email": "agent@example.com"}, key=PROVISION)
    status, reg = call("POST", "/platform/register", {"username": "agent", "email": "agent@example.com"}, key=PROVISION)
    assert reg["user_id"] == out["user_id"]


def test_admin_accounts_cant_sign_in_on_the_website_with_a_wallet(env):
    uid = _email_account(env)
    _link_for_sign_in(env, uid, EVM_ADDR.lower())
    from greencompute_persistence.orm import AdminAccountORM

    with session_scope(env.sf) as s:
        s.add(AdminAccountORM(user_id=uid, email="ada@example.com", added_at=datetime.now(UTC)))
    ch = challenge(EVM_ADDR)
    status, body = call("POST", "/platform/auth/wallet/website-session",
                        {"nonce": ch["nonce"], "signature": evm_sign(ch["message"])}, key=PROVISION)
    assert status == 403 and "email or Google" in body["error"]["message"]


def test_adding_an_admin_by_an_email_two_accounts_share_is_refused(env):
    from greencompute_gateway.application.services import GatewayService

    _email_account(env, "a1", "dup@example.com")
    with session_scope(env.sf) as s:
        s.add(UserORM(user_id="a2", username="dup2", email="DUP@example.com", balance_credits=0, profile_metadata={}))
    svc = GatewayService(repository=routes.service.repository)
    with pytest.raises(ValueError, match="more than one"):
        svc.add_admin("dup@example.com", added_by="k")


def test_a_flagged_wallet_cant_be_unlinked_to_lift_the_block(env):
    uid = _email_account(env)
    other = keys.PrivateKey(bytes.fromhex("c1" * 32))
    addr = other.public_key.to_checksum_address().lower()
    _link_for_sign_in(env, uid, addr)
    issued = sign_in_evm(other)
    sanctions.store_list(env.sf, sanctions.OFAC, {("evm", addr): "Listed", ("evm", LISTED_EVM): "x"})
    [w] = env.billing.repo.list_verified_wallets(uid)
    status, body = call("DELETE", f"/platform/billing/wallets/{w['wallet_id']}", key=issued["api_key"])
    assert status == 409 and "support@green-compute.com" in body["error"]["message"]
    assert sanctions.account_blocked(env.sf, uid) == "list"



def test_a_wallet_linked_to_pay_in_signs_in_only_once_its_owner_turns_that_on(env):
    """A payments wallet isn't a way in by default: a signature phished from it
    (Bittensor extensions don't check the site) must not open the account."""
    uid = _email_account(env)
    wallet, _ = env.billing.repo.add_verified_wallet(uid, "evm", EVM_ADDR.lower())
    ch = challenge(EVM_ADDR)
    status, body = verify(ch["nonce"], evm_sign(ch["message"]))
    assert status == 403 and body["error"]["code"] == "wallet_sign_in_off"
    assert "Billing > Verified wallets" in body["error"]["message"]
    assert [w["sign_in"] for w in env.billing.repo.list_verified_wallets(uid)] == [False]

    env.billing.repo.set_wallet_sign_in(uid, wallet["wallet_id"], True)  # the owner turns it on
    out = sign_in_evm()
    assert out["user_id"] == uid

    # Off again: the keys its sign-ins issued stop working, and it can't sign in.
    result = env.billing.repo.set_wallet_sign_in(uid, wallet["wallet_id"], False)
    assert result["revoked_key_ids"] == [out["key_id"]]
    assert call("GET", "/platform/billing/balance", key=out["api_key"])[0] == 401
    ch = challenge(EVM_ADDR)
    assert verify(ch["nonce"], evm_sign(ch["message"]))[0] == 403


def test_a_wallet_accounts_own_wallet_signs_in_and_cant_be_switched_off_alone(env):
    out = sign_in_evm()
    [wallet] = env.billing.repo.list_verified_wallets(out["user_id"])
    assert wallet["sign_in"] is True
    status, body = call("PATCH", f"/platform/billing/wallets/{wallet['wallet_id']}", {"sign_in": False}, key=out["api_key"])
    assert status == 409 and "signs in" in body["error"]["message"]
    status, body = call("PATCH", f"/platform/billing/wallets/{wallet['wallet_id']}", {"sign_in": "yes"}, key=out["api_key"])
    assert status == 400
    status, _ = call("PATCH", "/platform/billing/wallets/not-mine", {"sign_in": True}, key=out["api_key"])
    assert status == 404
    # A second wallet, linked to pay in: the owner turns signing in on over the
    # API, approved by a signature from the wallet that signs in already.
    second = keys.PrivateKey(bytes.fromhex("c3" * 32))
    w2, _ = env.billing.repo.add_verified_wallet(out["user_id"], "evm", second.public_key.to_checksum_address().lower())
    body = _approve_sign_in(EVM_KEY, w2)
    status, result = call("PATCH", f"/platform/billing/wallets/{w2['wallet_id']}", body, key=out["api_key"])
    assert status == 200 and result["sign_in"] is True and result["revoked_key_ids"] == []
    assert sign_in_evm(second)["user_id"] == out["user_id"]


# --- security review round 1 -----------------------------------------------------------------


def test_keys_made_with_a_wallet_key_go_when_the_wallet_is_shut_out(env):
    """A phished signature must not leave a key behind that outlives signing in
    again, revoking, unlinking or turning the wallet's sign-in off."""
    first = sign_in_evm()
    status, child = call("POST", "/platform/api-keys", {"name": "made by the agent"}, key=first["api_key"])
    assert status == 200, child
    assert call("GET", "/platform/billing/balance", key=child["secret"])[0] == 200
    again = sign_in_evm()  # the owner signs in again after a leak
    assert set(again["revoked_key_ids"]) == {first["key_id"], child["key_id"]}
    assert call("GET", "/platform/billing/balance", key=child["secret"])[0] == 401


def test_unlinking_the_last_wallet_that_signs_in_is_refused_even_with_a_payments_wallet(env):
    out = sign_in_evm()
    second = keys.PrivateKey(bytes.fromhex("d4" * 32))
    env.billing.repo.add_verified_wallet(out["user_id"], "evm", second.public_key.to_checksum_address().lower())
    wallets = {w["sign_in"]: w for w in env.billing.repo.list_verified_wallets(out["user_id"])}
    status, body = call("DELETE", f"/platform/billing/wallets/{wallets[True]['wallet_id']}", key=out["api_key"])
    assert status == 409 and "signs in" in body["error"]["message"]
    # The payments-only one can go: it was never a way in.
    status, _ = call("DELETE", f"/platform/billing/wallets/{wallets[False]['wallet_id']}", key=out["api_key"])
    assert status == 200


def test_a_key_name_that_would_silence_the_sign_in_notice_is_made_printable(env):
    uid = _email_account(env)
    _link_for_sign_in(env, uid, EVM_ADDR.lower())
    out = sign_in_evm(key_name="café ☃\n")
    assert out["key_name"] == "caf"
    assert env.emails[-1][2]["key_name"] == "caf"
    assert sign_in_evm(key_name="☃☃")["key_name"] == wallet_auth.DEFAULT_KEY_NAME


def test_live_oracle_calls_are_capped_per_minute(monkeypatch):
    calls = []
    monkeypatch.setattr(sanctions.sanctions_lists, "oracle_is_sanctioned", lambda a, **k: calls.append(a) or False)
    monkeypatch.setattr(sanctions, "ORACLE_MAX_PER_MINUTE", 2)
    sanctions._oracle_cache.clear()
    sanctions._oracle_calls.clear()
    sanctions._oracle_failed_at[:] = []
    answers = [sanctions.oracle_says(f"0x{n:040x}") for n in range(3)]
    assert answers == [False, False, None] and len(calls) == 2  # the third: the lists decide


def test_a_sanctions_blocked_account_cant_use_paid_inference(env):
    out = sign_in_evm()
    routes._BLOCKED_CACHE.clear()
    sanctions.store_list(env.sf, sanctions.OFAC, {("evm", EVM_ADDR.lower()): "Listed", ("evm", LISTED_EVM): "x"})
    status, body = call("POST", "/v1/chat/completions",
                        {"model": "any", "messages": [{"role": "user", "content": "hi"}]}, key=out["api_key"])
    assert status == 403 and body["error"]["code"] == "wallet_blocked"


def test_a_website_wallet_session_lasts_only_while_the_wallet_may_sign_in(env):
    ch = challenge(EVM_ADDR)
    status, sess = call("POST", "/platform/auth/wallet/website-session",
                        {"nonce": ch["nonce"], "signature": evm_sign(ch["message"])}, key=PROVISION)
    assert status == 200
    ask = {"user_id": sess["user_id"], "chain": "evm", "address": EVM_ADDR}

    def ok(body=ask, key=PROVISION):
        return call("POST", "/platform/auth/wallet/website-session/check", body, key=key)

    assert ok() == (200, {"ok": True})
    assert ok({**ask, "user_id": "someone-else"})[1] == {"ok": False}
    assert ok(key=sess.get("api_key") or "gk_nope")[0] in (401, 403)  # the website's secret only
    # The owner adds a second way in, then turns this wallet's sign-in off: the session ends.
    second = keys.PrivateKey(bytes.fromhex("e5" * 32))
    w2, _ = env.billing.repo.add_verified_wallet(sess["user_id"], "evm", second.public_key.to_checksum_address().lower())
    w1 = next(w for w in env.billing.repo.list_verified_wallets(sess["user_id"]) if w["address"] == EVM_ADDR.lower())
    env.billing.repo.set_wallet_sign_in(sess["user_id"], w2["wallet_id"], True, approved_by=f"evm:{w1['address']}")
    env.billing.repo.set_wallet_sign_in(sess["user_id"], w1["wallet_id"], False)
    assert ok()[1] == {"ok": False}
    # Listed by sanctions screening: ended too.
    env.billing.repo.set_wallet_sign_in(sess["user_id"], w1["wallet_id"], True, approved_by=f"evm:{w2['address']}")
    assert ok()[1] == {"ok": True}
    sanctions.store_list(env.sf, sanctions.OFAC, {("evm", EVM_ADDR.lower()): "Listed", ("evm", LISTED_EVM): "x"})
    assert ok()[1] == {"ok": False}


# --- security review round 2: website session keys, approvals ---------------------------


def _web_session(key=EVM_KEY):
    ch = challenge(key.public_key.to_checksum_address())
    status, out = call("POST", "/platform/auth/wallet/website-session",
                       {"nonce": ch["nonce"], "signature": evm_sign(ch["message"], key)}, key=PROVISION)
    assert status == 200, out
    return out


def _works(secret):
    return call("GET", "/platform/billing/balance", key=secret)[0] == 200


def test_each_website_session_has_its_own_key_that_an_agent_sign_in_leaves_alone(env):
    web = _web_session()
    status, sub = call("POST", "/platform/api-keys", {"name": "made on the website"}, key=web["api_key"])
    assert status == 200
    with session_scope(env.sf) as s:
        assert s.get(APIKeyORM, sub["key_id"]).issued_by_wallet == f"evm:{EVM_ADDR.lower()}#web"
    agent = sign_in_evm()
    again = sign_in_evm()  # an agent replacing its key logs no person out
    assert again["revoked_key_ids"] == [agent["key_id"]]
    assert _works(web["api_key"]) and _works(sub["secret"])


def test_a_revoke_keys_signature_ends_website_sessions_too(env):
    web, agent = _web_session(), sign_in_evm()
    ch = challenge(EVM_ADDR, action="revoke_keys")
    status, out = verify(ch["nonce"], evm_sign(ch["message"]))
    assert status == 200 and set(out["revoked_key_ids"]) == {web["key_id"], agent["key_id"]}
    assert not _works(web["api_key"]) and not _works(agent["api_key"])


def test_unlinking_or_turning_sign_in_off_ends_the_wallets_website_sessions(env):
    uid = _email_account(env)
    w = _link_for_sign_in(env, uid, EVM_ADDR.lower())
    web = _web_session()
    status, sub = call("POST", "/platform/api-keys", {"name": "planted"}, key=web["api_key"])
    off = env.billing.repo.set_wallet_sign_in(uid, w["wallet_id"], False)
    assert set(off["revoked_key_ids"]) == {web["key_id"], sub["key_id"]}
    assert not _works(web["api_key"]) and not _works(sub["secret"])
    env.billing.repo.set_wallet_sign_in(uid, w["wallet_id"], True)  # the only sign-in wallet: no approval
    web = _web_session()
    assert env.billing.repo.unlink_wallet(uid, w["wallet_id"])["revoked_key_ids"] == [web["key_id"]]
    assert not _works(web["api_key"])


def test_a_wallet_keeps_only_its_five_newest_website_sessions(env, monkeypatch):
    sessions = [_web_session() for _ in range(6)]
    status, sub = call("POST", "/platform/api-keys", {"name": "made in session 6"}, key=sessions[-1]["api_key"])
    sessions.append(_web_session())
    assert [_works(s["api_key"]) for s in sessions] == [False, False, True, True, True, True, True]
    assert _works(sub["secret"])  # keys made with a session's key don't count


def test_a_sanctions_hit_ends_the_website_session_and_its_key(env):
    web = _web_session()
    sanctions.store_list(env.sf, sanctions.OFAC, {("evm", EVM_ADDR.lower()): "Listed", ("evm", LISTED_EVM): "x"})
    status, out = call("POST", "/platform/auth/wallet/website-session/check",
                       {"user_id": web["user_id"], "chain": "evm", "address": EVM_ADDR}, key=PROVISION)
    assert out == {"ok": False} and not _works(web["api_key"])


def test_turning_on_another_wallet_needs_a_sign_in_wallets_signature(env):
    out = sign_in_evm()  # a wallet account: EVM_KEY signs in to it
    second = keys.PrivateKey(bytes.fromhex("d4" * 32))
    w2, _ = env.billing.repo.add_verified_wallet(out["user_id"], "evm", second.public_key.to_checksum_address().lower())
    path = f"/platform/billing/wallets/{w2['wallet_id']}"
    status, body = call("PATCH", path, {"sign_in": True}, key=out["api_key"])
    assert status == 403 and body["error"]["code"] == "approval_required"
    assert body["detail"]["sign_with"] == [{"chain": "evm", "address": EVM_ADDR.lower()}]
    assert body["detail"]["subject"] == f"evm:{w2['address']}"
    # The message names the wallet it lets in, in words.
    ch = challenge(EVM_ADDR, action="approve_sign_in", subject=f"evm:{w2['address'].upper().replace('0X', '0x')}")
    assert f"Allow the Ethereum/Base wallet {second.public_key.to_checksum_address()} to sign in" in ch["message"]
    assert ch["subject"] == f"evm:{w2['address']}"
    # Signed by the wallet being let in itself: not one that signs in yet.
    status, body = call("PATCH", path, _approve_sign_in(second, w2), key=out["api_key"])
    assert status == 403 and body["error"]["code"] == "approval_required"
    # Approval for a different wallet doesn't carry over.
    third = keys.PrivateKey(bytes.fromhex("d5" * 32))
    w3, _ = env.billing.repo.add_verified_wallet(out["user_id"], "evm", third.public_key.to_checksum_address().lower())
    status, body = call("PATCH", path, _approve_sign_in(EVM_KEY, w3), key=out["api_key"])
    assert status == 400 and body["error"]["code"] == "challenge_invalid"
    # A limited key can't do it even with the signature.
    status, limited = call("POST", "/platform/api-keys", {"name": "billing", "scopes": ["billing", "read"]},
                           key=out["api_key"])
    status, body = call("PATCH", path, _approve_sign_in(EVM_KEY, w2), key=limited["secret"])
    assert status == 403 and body["error"]["code"] == "scope_required"
    approval = _approve_sign_in(EVM_KEY, w2)
    assert call("PATCH", path, approval, key=out["api_key"])[0] == 200
    assert call("PATCH", f"/platform/billing/wallets/{w3['wallet_id']}", {**approval, "sign_in": True},
                key=out["api_key"])[1]["error"]["code"] == "challenge_invalid"  # used once


def test_another_accounts_wallet_cant_approve(env):
    victim = sign_in_evm()
    mallory = keys.PrivateKey(bytes.fromhex("d6" * 32))
    sign_in_evm(mallory)  # signs in to its own account, not the victim's
    planted = keys.PrivateKey(bytes.fromhex("d7" * 32))
    w, _ = env.billing.repo.add_verified_wallet(victim["user_id"], "evm", planted.public_key.to_checksum_address().lower())
    status, body = call("PATCH", f"/platform/billing/wallets/{w['wallet_id']}", _approve_sign_in(mallory, w),
                        key=victim["api_key"])
    assert status == 403 and "doesn't sign in to this account" in body["error"]["message"]


def test_the_first_sign_in_wallet_of_an_email_account_needs_no_signature_and_the_owner_is_told(env):
    uid = _email_account(env)
    w, _ = env.billing.repo.add_verified_wallet(uid, "evm", EVM_ADDR.lower())
    status, key = call("POST", "/platform/api-keys", {"name": "default", "user_id": uid}, key=PROVISION)
    status, body = call("PATCH", f"/platform/billing/wallets/{w['wallet_id']}", {"sign_in": True}, key=key["secret"])
    assert status == 200 and body["changed"] is True
    assert [(e[1], e[2]["event"]) for e in env.emails] == [("wallet_sign_in", "sign_in_on")]


def test_an_approval_can_never_sign_in(env):
    out = sign_in_evm()
    for action, subject in (("approve_sign_in", "evm:" + "0x" + "ee" * 20), ("add_email", "a@b.io")):
        ch = challenge(EVM_ADDR, action=action, subject=subject)
        status, body = verify(ch["nonce"], evm_sign(ch["message"]))
        assert status == 400 and body["error"]["code"] == "challenge_invalid"
        ch = challenge(EVM_ADDR, action=action, subject=subject)
        status, body = call("POST", "/platform/auth/wallet/website-session",
                            {"nonce": ch["nonce"], "signature": evm_sign(ch["message"])}, key=PROVISION)
        assert status == 400 and body["error"]["code"] == "challenge_invalid"
    assert call("POST", "/platform/auth/wallet/challenge", {"address": EVM_ADDR, "action": "approve_sign_in"})[0] == 400
    assert call("POST", "/platform/auth/wallet/challenge",
                {"address": EVM_ADDR, "action": "sign_in", "subject": "x"})[0] == 400
    assert out


def test_a_wallet_account_adding_an_email_needs_its_wallets_signature(env):
    out = sign_in_evm()

    def ask(body):
        return call("POST", "/platform/account/email-approval", {"user_id": out["user_id"], **body}, key=PROVISION)

    status, body = ask({"email": "me@example.com"})
    assert status == 403 and body["error"]["code"] == "approval_required"
    ch = challenge(EVM_ADDR, action="add_email", subject="Me@Example.com")
    assert "Add the email address me@example.com to your Green Compute account" in ch["message"]
    sig = evm_sign(ch["message"])
    status, body = ask({"email": "other@example.com", "nonce": ch["nonce"], "signature": sig})
    assert status == 400 and body["error"]["code"] == "challenge_invalid"  # signed for another email
    ch = challenge(EVM_ADDR, action="add_email", subject="me@example.com")
    approval = {"email": "me@example.com", "nonce": ch["nonce"], "signature": evm_sign(ch["message"])}
    assert ask(approval) == (200, {"approved": True, "needed": True, "signed_by": f"evm:{EVM_ADDR.lower()}"})
    assert ask(approval)[1]["error"]["code"] == "challenge_invalid"  # once
    stranger = keys.PrivateKey(bytes.fromhex("d8" * 32))
    ch = challenge(stranger.public_key.to_checksum_address(), action="add_email", subject="me@example.com")
    status, body = ask({"email": "me@example.com", "nonce": ch["nonce"], "signature": evm_sign(ch["message"], stranger)})
    assert status == 403
    assert call("POST", "/platform/account/email-approval", {"user_id": out["user_id"], "email": "me@example.com"},
                key=out["api_key"])[0] in (401, 403)
    # An account made on the website needs no signature.
    uid = _email_account(env)
    status, body = call("POST", "/platform/account/email-approval", {"user_id": uid, "email": "x@example.com"},
                        key=PROVISION)
    assert body == {"approved": True, "needed": False}


# --- security review round 3 -----------------------------------------------------------------


def test_a_key_minted_after_its_parent_was_shut_out_is_refused_and_deleted(env, monkeypatch):
    """The race: the wallet is shut out between the mint's key check and its
    tagging. The new key must not survive untagged."""
    uid = _email_account(env)
    _link_for_sign_in(env, uid, EVM_ADDR.lower())
    first = sign_in_evm()
    real = routes.service.create_api_key

    def create_then_shut_out(*a, **k):
        created = real(*a, **k)
        [w] = env.billing.repo.list_verified_wallets(uid)
        env.billing.repo.set_wallet_sign_in(uid, w["wallet_id"], False)  # the owner, in between
        return created

    monkeypatch.setattr(routes.service, "create_api_key", create_then_shut_out)
    status, _ = call("POST", "/platform/api-keys", {"name": "planted"}, key=first["api_key"])
    assert status == 401
    from greencompute_persistence.orm import APIKeyORM

    with session_scope(env.sf) as s:
        assert s.scalars(select(APIKeyORM).where(APIKeyORM.name == "planted")).all() == []


def test_a_wallet_cant_be_turned_on_with_a_key_shut_out_mid_request(env):
    uid = _email_account(env)
    _link_for_sign_in(env, uid, EVM_ADDR.lower())
    phished = sign_in_evm()
    attacker = keys.PrivateKey(bytes.fromhex("f6" * 32))
    w_att, _ = env.billing.repo.add_verified_wallet(uid, "evm", attacker.public_key.to_checksum_address().lower())
    [w_v] = [w for w in env.billing.repo.list_verified_wallets(uid) if w["address"] == EVM_ADDR.lower()]
    env.billing.repo.set_wallet_sign_in(uid, w_v["wallet_id"], False)  # the owner shuts V out first...
    from greencompute_gateway.infrastructure.billing_repository import CallerShutOut

    with pytest.raises(CallerShutOut):  # ...so V's key can't turn the attacker's wallet on
        env.billing.repo.set_wallet_sign_in(uid, w_att["wallet_id"], True, caller_key_id=phished["key_id"])
    assert all(not w["sign_in"] for w in env.billing.repo.list_verified_wallets(uid))


def test_a_wallet_session_needs_an_approval_to_add_an_email_to_any_account(env):
    with session_scope(env.sf) as s:  # a GitHub account with a private email: none on file
        s.add(UserORM(user_id="gh-user", username="gh", email=None, balance_credits=0, profile_metadata={},
                      signup_method="website"))
    status, body = call("POST", "/platform/account/email-approval",
                        {"user_id": "gh-user", "email": "x@evil.test", "wallet_session": True}, key=PROVISION)
    assert status == 403 and body["error"]["code"] == "approval_required"
    status, body = call("POST", "/platform/account/email-approval",
                        {"user_id": "gh-user", "email": "x@evil.test"}, key=PROVISION)
    assert status == 200 and body == {"approved": True, "needed": False}

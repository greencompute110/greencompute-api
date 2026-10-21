"""Payment emails: every path that adds a payment to a balance asks the
website to email the user once, never for a transfer it had already credited;
reviews send their own emails; and the email can never get in the way of the
credit. The website POST is mocked: nothing leaves the test."""
import asyncio
import json
import logging
import types

import pytest
from substrateinterface import Keypair

from greencompute_persistence import session_scope
from greencompute_persistence.orm import CryptoInvoiceORM, UserORM
from greencompute_protocol import StripeSession
from greencompute_gateway.application.billing_service import BillingService
from greencompute_gateway.infrastructure import notify_website as nw
from greencompute_gateway.infrastructure import price_feed, stripe_client
from greencompute_gateway.infrastructure.billing_repository import BillingRepository

TAO_ADDR = Keypair.create_from_uri("//GreenComputeDeposit").ss58_address
USDC_ADDR = "0x" + "ab" * 20
PRICES = {"tao": 300.0, "alpha": 5.0}
SECRET = "provision-secret-for-tests"
NOTIFY_URL = "https://site.test/api/internal/notify"


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.setenv("BILLING_DEPOSIT_TAO", TAO_ADDR)
    monkeypatch.setenv("BILLING_DEPOSIT_USDC_BASE", USDC_ADDR)
    for name in ("BILLING_DEPOSIT_ALPHA", "BILLING_DEPOSIT_USDC", "BILLING_DEPOSIT_USDT",
                 "BILLING_DEPOSIT_USDT_BASE", "BILLING_DEPOSIT_USDT_ETH", "BILLING_DEPOSIT_USDC_ETH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(price_feed, "get_price", lambda c: PRICES.get(c, 0.0))
    repo = BillingRepository(database_url="sqlite:///:memory:", bootstrap=True)
    with session_scope(repo.session_factory) as s:
        s.add(UserORM(user_id="alice", username="alice", display_name="Alice Smith", email="alice@x.io", balance_credits=0))
        s.add(UserORM(user_id="bob", username="bob", email="bob@x.io", balance_credits=500))
        s.add(UserORM(user_id="noemail", username="agent-7", email=None, balance_credits=0))
        s.add(UserORM(user_id="gone", username="deleted-0a1b2c3d4e5f", email="old@x.io", balance_credits=0))
    return BillingService(repo)


@pytest.fixture
def sent(monkeypatch):
    """Payment emails on, delivered inline; each POST to the website recorded."""
    monkeypatch.setenv("GREENCOMPUTE_PROVISION_SECRET", SECRET)
    monkeypatch.setenv("GREENCOMPUTE_PAYMENT_EMAILS_ENABLED", "true")
    monkeypatch.setenv("WEBSITE_NOTIFY_URL", NOTIFY_URL)
    monkeypatch.setattr(nw.threading, "Thread", lambda target, args, **kw: types.SimpleNamespace(start=lambda: target(*args)))
    monkeypatch.setattr(nw.time, "sleep", lambda s: None)
    posts = []

    def post(url, secret, data):
        posts.append(json.loads(data))
        assert (url, secret) == (NOTIFY_URL, SECRET)
        return 200

    monkeypatch.setattr(nw, "_post", post)
    return posts


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


def units(svc, invoice_id):
    with session_scope(svc.repo.session_factory) as s:
        return s.get(CryptoInvoiceORM, invoice_id).amount_units


def expire(svc, invoice_id):
    with session_scope(svc.repo.session_factory) as s:
        s.get(CryptoInvoiceORM, invoice_id).status = "expired"


def link(svc, user, keypair):
    ch = svc.wallet_challenge(user, "bittensor", keypair.ss58_address)
    return svc.link_wallet(user, "bittensor", keypair.ss58_address, ch["issued_at"],
                           "0x" + keypair.sign(ch["message"]).hex())


def stripe_session(svc, user="alice", sid="cs_test_1", usd=25.0):
    svc.repo.create_stripe_session(StripeSession(
        user_id=user, stripe_session_id=sid, amount_usd=usd, amount_cents=int(usd * 100),
    ))


@pytest.fixture
def api(svc, monkeypatch):
    from greencompute_gateway.transport import routes

    monkeypatch.setattr(routes, "require_api_key", lambda *a, **k: types.SimpleNamespace(user_id="alice"))
    monkeypatch.setattr(routes, "_get_billing", lambda: svc)
    monkeypatch.setattr("greencompute_gateway.infrastructure.alerts.send_payment_alert", lambda text: None)

    def call(name, *args):
        return getattr(routes, name)(*args, authorization="Bearer k", x_api_key=None)

    return types.SimpleNamespace(call=call, routes=routes)


# --- card payments ---------------------------------------------------------------


def test_stripe_webhook_emails_once_per_payment(svc, sent, api, monkeypatch):
    stripe_session(svc)
    event = {"type": "checkout.session.completed",
             "data": {"object": {"id": "cs_test_1", "payment_status": "paid", "payment_intent": "pi_1"}}}
    monkeypatch.setattr(stripe_client, "verify_webhook_signature", lambda payload, sig: event)
    request = types.SimpleNamespace(body=lambda: asyncio.sleep(0, b"{}"), headers={"stripe-signature": "t=1"})

    asyncio.run(api.routes.billing_stripe_webhook(request))
    asyncio.run(api.routes.billing_stripe_webhook(request))  # Stripe re-delivers

    assert len(sent) == 1
    email = sent[0]
    assert email["type"] == "payment_received" and email["event_id"] == "payment:stripe:cs_test_1"
    assert (email["to"], email["name"]) == ("alice@x.io", "Alice Smith")
    assert (email["amount_usd"], email["method"], email["balance_usd"]) == (25.0, "card", 25.0)
    assert email["currency"] is None and email["tx_hash"] is None and email["via_review"] is False
    assert email["credited_at"].endswith("Z")


def test_stripe_reconcile_emails_and_a_late_webhook_does_not(svc, sent, monkeypatch):
    stripe_session(svc, user="bob", sid="cs_test_2", usd=10.0)
    monkeypatch.setattr(stripe_client, "retrieve_checkout_session",
                        lambda sid: {"payment_status": "paid", "status": "complete", "payment_intent": "pi_2"})
    svc.reconcile_pending_stripe_sessions(dry_run=False)
    svc.reconcile_pending_stripe_sessions(dry_run=False)
    svc.confirm_stripe_payment("cs_test_2", payment_intent_id="pi_2")  # the webhook, late
    assert [(e["to"], e["amount_usd"], e["balance_usd"]) for e in sent] == [("bob@x.io", 10.0, 15.0)]


def test_unpaid_stripe_session_sends_nothing(svc, sent, monkeypatch):
    stripe_session(svc)
    monkeypatch.setattr(stripe_client, "retrieve_checkout_session",
                        lambda sid: {"payment_status": "unpaid", "status": "open"})
    svc.reconcile_pending_stripe_sessions(dry_run=False)
    assert sent == []


# --- crypto: the watcher -------------------------------------------------------------


def test_paid_invoice_emails_once(svc, sent):
    inv = svc.create_crypto_invoice("alice", "usdc-base", 20)
    d = deposit("usdc-base:0xpaid:0", amount_raw=units(svc, inv["invoice_id"]), tx="0xpaid")
    assert svc.resolve_deposit(d) == "invoice"
    assert svc.resolve_deposit(d) == "seen"

    [email] = sent
    assert email["type"] == "payment_received" and email["event_id"] == "payment:usdc-base:0xpaid:0"
    assert (email["method"], email["currency"], email["tx_hash"]) == ("crypto", "usdc-base", "0xpaid")
    assert email["crypto_amount"] == inv["amount_to_send"]  # "20.00xxxx", as sent
    assert (email["amount_usd"], email["balance_usd"], email["bonus_pct"]) == (20.0, 20.0, 0.0)


def test_overpaid_and_late_invoices_email_what_was_added(svc, sent):
    over = svc.create_crypto_invoice("alice", "usdc-base", 20)
    assert svc.resolve_deposit(deposit("usdc-base:0x1:0", amount_raw=20_300_000)) == "invoice_overpay"
    late = svc.create_crypto_invoice("bob", "usdc-base", 10)
    expire(svc, late["invoice_id"])
    assert svc.resolve_deposit(deposit("usdc-base:0x2:0", amount_raw=units(svc, late["invoice_id"]))) == "invoice_late"
    assert over["invoice_id"] != late["invoice_id"]
    assert [(e["to"], e["amount_usd"], e["crypto_amount"]) for e in sent] == [
        ("alice@x.io", 20.3, "20.300000"),
        # Late: valued when it arrived, so the few extra millionths count too.
        ("bob@x.io", (svc.repo.get_balance("bob") - 500) / 100, f"{units(svc, late['invoice_id']) / 1e6:.6f}"),
    ]


def test_verified_wallet_payment_emails_with_the_alpha_bonus(svc, sent):
    payer = Keypair.create_from_uri("//AliceAgent")
    link(svc, "alice", payer)
    d = deposit("alpha:9:1", "alpha", 2_000_000_000, sender=payer.ss58_address, tx="0xext")
    assert svc.resolve_deposit(d) == "wallet"
    svc.resolve_deposit(d)
    [email] = sent
    assert (email["amount_usd"], email["crypto_amount"], email["currency"], email["bonus_pct"]) == (11.0, "2.000000", "alpha", 0.1)


def test_unmatched_and_dust_payments_send_nothing(svc, sent, monkeypatch):
    monkeypatch.setattr("greencompute_gateway.infrastructure.alerts.send_payment_alert", lambda text: None)
    assert svc.resolve_deposit(deposit("usdc-base:0x3:0", amount_raw=7_000_000)) == "unmatched"
    assert svc.resolve_deposit(deposit("tao:1:1", "tao", 10, sender="5Dust")) == "ignored"
    assert sent == []


def test_linking_a_wallet_emails_each_claimed_payment_once(svc, sent, monkeypatch):
    monkeypatch.setattr("greencompute_gateway.infrastructure.alerts.send_payment_alert", lambda text: None)
    payer = Keypair.create_from_uri("//BobLaptop")
    svc.resolve_deposit(deposit("tao:7:1", "tao", 100_000_000, sender=payer.ss58_address, tx="0xa"))
    svc.resolve_deposit(deposit("tao:7:2", "tao", 50_000_000, sender=payer.ss58_address, tx="0xb"))
    assert sent == []
    link(svc, "bob", payer)
    link(svc, "bob", payer)  # linking again claims nothing new
    assert [(e["event_id"], e["amount_usd"]) for e in sent] == [("payment:tao:7:1", 30.0), ("payment:tao:7:2", 15.0)]
    assert sent[-1]["balance_usd"] == 50.0


# --- crypto: admins ------------------------------------------------------------------


def test_admin_assigning_a_deposit_emails_once(svc, sent, monkeypatch):
    monkeypatch.setattr("greencompute_gateway.infrastructure.alerts.send_payment_alert", lambda text: None)
    svc.resolve_deposit(deposit("usdc-base:0x9:0", amount_raw=12_340_000, tx="0x9"))
    assert svc.assign_deposit("usdc-base:0x9:0", "bob")["credited"]
    assert svc.assign_deposit("usdc-base:0x9:0", "alice")["already_credited"]
    [email] = sent
    assert (email["to"], email["amount_usd"], email["balance_usd"], email["tx_hash"]) == ("bob@x.io", 12.34, 17.34, "0x9")


def test_admin_confirming_an_invoice_emails_once(svc, sent):
    inv = svc.create_crypto_invoice("alice", "tao", 30)
    assert svc.confirm_crypto_deposit(inv["invoice_id"], "0xabc")["credited"]
    svc.confirm_crypto_deposit(inv["invoice_id"], "0xabc")
    [email] = sent
    assert email["event_id"] == f"payment:invoice:{inv['invoice_id']}"
    assert (email["amount_usd"], email["crypto_amount"], email["tx_hash"], email["via_review"]) == (30.0, inv["amount_to_send"], "0xabc", False)


def test_admin_confirm_of_a_reviewed_invoice_says_so(svc, sent, api):
    inv = svc.create_crypto_invoice("alice", "usdc-base", 20)
    api.call("billing_request_review", inv["invoice_id"], {"tx_hash": "0xmine"})
    svc.confirm_crypto_deposit(inv["invoice_id"], "0xmine")
    assert [e["type"] for e in sent] == ["review_received", "payment_received"]
    assert sent[1]["via_review"] is True


# --- reviews -----------------------------------------------------------------------------


def test_requesting_a_review_emails_what_happens_next(svc, sent, api):
    inv = svc.create_crypto_invoice("alice", "alpha", 25)
    expire(svc, inv["invoice_id"])
    api.call("billing_request_review", inv["invoice_id"], {"tx_hash": "0xsent", "note": "from an exchange"})
    [email] = sent
    assert email["type"] == "review_received" and email["to"] == "alice@x.io"
    assert (email["amount_usd"], email["currency"], email["crypto_amount"], email["tx_hash"]) == (
        25.0, "alpha", inv["amount_to_send"], "0xsent")


def test_review_credited_from_a_deposit_sends_one_email(svc, sent, api, monkeypatch):
    monkeypatch.setattr("greencompute_gateway.infrastructure.alerts.send_payment_alert", lambda text: None)
    inv = svc.create_crypto_invoice("alice", "usdc-base", 20)
    svc.resolve_deposit(deposit("usdc-base:0xpaid:0", amount_raw=21_500_000, tx="0xPAID"))
    api.call("billing_request_review", inv["invoice_id"], {"tx_hash": "0xpaid"})
    sent.clear()

    api.call("billing_admin_resolve_review", inv["invoice_id"], {"action": "credit_deposit", "deposit_ref": "usdc-base:0xpaid:0"})

    [email] = sent  # the review's email only, not payment_received as well
    assert (email["type"], email["outcome"]) == ("review_resolved", "credited")
    assert (email["amount_usd"], email["balance_usd"], email["invoice_amount_usd"]) == (21.5, 21.5, 20.0)
    assert (email["crypto_amount"], email["tx_hash"]) == ("21.500000", "0xPAID")


def test_review_confirming_the_invoice_sends_one_email(svc, sent, api):
    inv = svc.create_crypto_invoice("bob", "tao", 30)
    expire(svc, inv["invoice_id"])
    with session_scope(svc.repo.session_factory) as s:
        s.get(CryptoInvoiceORM, inv["invoice_id"]).review_status = "open"
        s.get(CryptoInvoiceORM, inv["invoice_id"]).tx_hash = "0xLATE"
    api.call("billing_admin_resolve_review", inv["invoice_id"], {"action": "confirm_invoice"})
    [email] = sent
    assert (email["type"], email["outcome"], email["to"]) == ("review_resolved", "credited", "bob@x.io")
    assert (email["amount_usd"], email["balance_usd"], email["tx_hash"]) == (30.0, 35.0, "0xLATE")


def test_dismissed_review_says_we_could_not_match_it(svc, sent, api):
    inv = svc.create_crypto_invoice("alice", "usdc-base", 20)
    api.call("billing_request_review", inv["invoice_id"], {"tx_hash": "0xnope"})
    sent.clear()
    api.call("billing_admin_resolve_review", inv["invoice_id"], {"action": "dismiss"})
    [email] = sent
    assert (email["type"], email["outcome"], email["amount_usd"], email["balance_usd"]) == ("review_resolved", "dismissed", None, None)
    assert (email["invoice_amount_usd"], email["crypto_amount"], email["tx_hash"]) == (20.0, inv["amount_to_send"], "0xnope")


def test_dismissing_a_review_paid_some_other_way_sends_no_couldnt_match(svc, sent, api):
    payer = Keypair.create_from_uri("//AliceAgent")
    inv = svc.create_crypto_invoice("alice", "tao", 30)
    api.call("billing_request_review", inv["invoice_id"], {"tx_hash": "0xWRONGAMOUNT"})
    # Meanwhile she verifies the wallet she paid from: the payment is added.
    link(svc, "alice", payer)
    svc.resolve_deposit(deposit("tao:3:1", "tao", 90_000_000, sender=payer.ss58_address, tx="0xwrongamount"))
    sent.clear()
    api.call("billing_admin_resolve_review", inv["invoice_id"], {"action": "dismiss"})
    assert sent == []


# --- who gets one, and when not ------------------------------------------------------------


def test_no_email_without_an_address_or_for_a_deleted_account(svc, sent):
    for user in ("noemail", "gone"):
        inv = svc.create_crypto_invoice(user, "usdc-base", 5)
        assert svc.confirm_crypto_deposit(inv["invoice_id"], f"0x{user}")["credited"]
    assert sent == []
    assert svc.repo.get_balance("noemail") == 500 and svc.repo.get_balance("gone") == 500


@pytest.mark.parametrize("env", [
    {"GREENCOMPUTE_PAYMENT_EMAILS_ENABLED": "false"},
    {"GREENCOMPUTE_PROVISION_SECRET": ""},
])
def test_off_when_disabled_or_without_the_secret(svc, sent, monkeypatch, env):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    inv = svc.create_crypto_invoice("alice", "usdc-base", 20)
    assert svc.confirm_crypto_deposit(inv["invoice_id"], "0x1")["credited"]
    assert sent == [] and svc.repo.get_balance("alice") == 2000


def test_a_failing_email_never_touches_the_credit(svc, sent, monkeypatch, caplog):
    def broken(*a, **k):
        raise RuntimeError("no threads left")

    monkeypatch.setattr(nw.threading, "Thread", broken)
    inv = svc.create_crypto_invoice("alice", "usdc-base", 20)
    d = deposit("usdc-base:0x1:0", amount_raw=units(svc, inv["invoice_id"]))
    with caplog.at_level(logging.WARNING):
        assert svc.resolve_deposit(d) == "invoice"
    assert svc.repo.get_balance("alice") == 2000
    assert "payment email (payment_received) not sent" in caplog.text and "alice@x.io" not in caplog.text


def test_a_failing_lookup_or_website_never_touches_the_credit(svc, sent, monkeypatch, caplog):
    monkeypatch.setattr(nw, "_recipient", lambda *a: (_ for _ in ()).throw(RuntimeError("db down")))
    stripe_session(svc)
    with caplog.at_level(logging.WARNING):
        assert svc.confirm_stripe_payment("cs_test_1")["credited"]
    assert "user lookup failed" in caplog.text

    monkeypatch.setattr(nw, "_recipient", lambda *a: ("alice@x.io", "Alice"))
    monkeypatch.setattr(nw, "_post", lambda *a: 500)
    stripe_session(svc, sid="cs_test_2")
    assert svc.confirm_stripe_payment("cs_test_2")["credited"]
    assert svc.repo.get_balance("alice") == 5000


def test_sent_from_a_background_thread(svc, monkeypatch):
    import threading

    monkeypatch.setenv("GREENCOMPUTE_PROVISION_SECRET", SECRET)
    monkeypatch.setenv("GREENCOMPUTE_PAYMENT_EMAILS_ENABLED", "true")
    done = threading.Event()
    posted = []
    monkeypatch.setattr(nw, "_post", lambda url, secret, data: (posted.append((threading.current_thread().name, json.loads(data))), done.set(), 200)[2])
    stripe_session(svc)
    assert svc.confirm_stripe_payment("cs_test_1")["credited"]
    assert done.wait(5)
    [(thread, body)] = posted
    assert thread == "payment-email" and body["to"] == "alice@x.io"


def test_one_retry_when_the_website_is_unreachable(svc, monkeypatch, caplog):
    monkeypatch.setenv("GREENCOMPUTE_PROVISION_SECRET", SECRET)
    monkeypatch.setenv("GREENCOMPUTE_PAYMENT_EMAILS_ENABLED", "true")
    monkeypatch.setattr(nw.threading, "Thread", lambda target, args, **kw: types.SimpleNamespace(start=lambda: target(*args)))
    monkeypatch.setattr(nw.time, "sleep", lambda s: None)
    replies = {"unreachable": [None, 200], "down": [502, 503], "refused": [401]}
    for case, statuses in replies.items():
        calls = []
        monkeypatch.setattr(nw, "_post", lambda url, secret, data, s=statuses, c=calls: (c.append(json.loads(data)), s[len(c) - 1])[1])
        with caplog.at_level(logging.WARNING):
            nw.notify_user(svc.repo.session_factory, "alice", "payment_received", {"amount_usd": 1.0}, event_id="e1")
        assert len(calls) == len(statuses), case
        assert all(c["event_id"] == "e1" for c in calls)  # the website sends one email per event_id
    assert "website unreachable (503)" in caplog.text and "refused by the website: HTTP 401" in caplog.text
    assert "alice@x.io" not in caplog.text


def test_the_post_carries_the_secret_and_a_short_timeout(monkeypatch):
    seen = {}

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"sent": true}'

    def urlopen(req, timeout):
        seen.update(url=req.full_url, method=req.get_method(), secret=req.get_header("X-provision-secret"),
                    body=json.loads(req.data), timeout=timeout)
        return Resp()

    monkeypatch.setattr(nw.urllib.request, "urlopen", urlopen)
    assert nw._post(NOTIFY_URL, SECRET, json.dumps({"type": "payment_received"}).encode()) == 200
    assert seen == {"url": NOTIFY_URL, "method": "POST", "secret": SECRET,
                    "body": {"type": "payment_received"}, "timeout": 5.0}

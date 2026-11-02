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
    monkeypatch.setattr("greencompute_gateway.infrastructure.alerts.send_payment_alert", lambda text, **_: None)

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
    monkeypatch.setattr("greencompute_gateway.infrastructure.alerts.send_payment_alert", lambda text, **_: None)
    assert svc.resolve_deposit(deposit("usdc-base:0x3:0", amount_raw=7_000_000)) == "unmatched"
    assert svc.resolve_deposit(deposit("tao:1:1", "tao", 10, sender="5Dust")) == "ignored"
    assert sent == []


def test_linking_a_wallet_emails_each_claimed_payment_once(svc, sent, monkeypatch):
    monkeypatch.setattr("greencompute_gateway.infrastructure.alerts.send_payment_alert", lambda text, **_: None)
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
    monkeypatch.setattr("greencompute_gateway.infrastructure.alerts.send_payment_alert", lambda text, **_: None)
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
    monkeypatch.setattr("greencompute_gateway.infrastructure.alerts.send_payment_alert", lambda text, **_: None)
    inv = svc.create_crypto_invoice("alice", "usdc-base", 20)
    svc.create_crypto_invoice("bob", "usdc-base", 20)  # two customers fit: it waits for a person
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
    api.call("billing_admin_resolve_review", inv["invoice_id"], {"action": "dismiss", "reason": "tx_not_found"})
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
    api.call("billing_admin_resolve_review", inv["invoice_id"], {"action": "dismiss", "reason": "tx_not_found"})
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


def test_an_error_building_the_email_never_reaches_the_payment(monkeypatch):
    """The helpers run after the credit commits: even a bug in them (here a
    deposit missing fields) is logged, not raised into the credit path."""
    from greencompute_gateway.application import billing_service as bs

    svc = bs.BillingService.__new__(bs.BillingService)
    svc.repo = None
    assert svc._email_deposit("u1", {"deposit_ref": "x"}, 100, 200) is None
    assert svc._email_review_resolved({}, "credited") is None


# --- why a payment wasn't added, or was added at another amount ---------------------


def _invoice_entry(api, invoice_id):
    return next(i for i in api.call("billing_list_crypto_invoices") if i["invoice_id"] == invoice_id)


def test_dismissing_a_review_needs_a_reason_the_customer_is_told(svc, sent, api):
    inv = svc.create_crypto_invoice("alice", "usdc-base", 20)
    api.call("billing_request_review", inv["invoice_id"], {"tx_hash": "0xnope"})
    for body in ({"action": "dismiss"}, {"action": "dismiss", "reason": "bogus"},
                 {"action": "dismiss", "reason": "not_paid"},  # a reason for rejecting invoices only
                 {"action": "dismiss", "reason": "other"}):  # "other" needs the words
        with pytest.raises(Exception) as exc:
            api.call("billing_admin_resolve_review", inv["invoice_id"], body)
        assert getattr(exc.value, "status_code", None) == 409
    assert _invoice_entry(api, inv["invoice_id"])["review_status"] == "open"

    sent.clear()
    api.call("billing_admin_resolve_review", inv["invoice_id"],
             {"action": "dismiss", "reason": "tx_not_found", "note": "Hash ends in a typo?"})
    [email] = sent
    assert email["reason"] == ("We couldn't find that transaction on the Base network. "
                               "Note from our team: Hash ends in a typo?")
    assert email["next_step"].startswith("Check the transaction hash") and "on Base" in email["next_step"]

    entry = _invoice_entry(api, inv["invoice_id"])
    assert (entry["review_status"], entry["decision_reason"]) == ("dismissed", "tx_not_found")
    assert entry["status_message"] == f"We checked the payment you reported and didn't add it. {email['reason']}"
    assert entry["next_step"] == email["next_step"]


def test_other_reason_is_the_staff_note(svc, sent, api):
    inv = svc.create_crypto_invoice("alice", "tao", 30)
    api.call("billing_request_review", inv["invoice_id"], {"tx_hash": "0xmixer"})
    sent.clear()
    api.call("billing_admin_resolve_review", inv["invoice_id"],
             {"action": "dismiss", "reason": "other", "note": "The transfer was reversed by your exchange."})
    [email] = sent
    assert email["reason"] == "The transfer was reversed by your exchange."
    assert "support@green-compute.com" in email["next_step"]


def test_rejecting_an_invoice_needs_a_reason_and_emails_it_once(svc, sent, api):
    inv = svc.create_crypto_invoice("alice", "alpha", 25)
    with pytest.raises(Exception) as exc:
        api.call("billing_reject_crypto", inv["invoice_id"], {})
    assert exc.value.status_code == 400
    with pytest.raises(Exception) as exc:
        api.call("billing_reject_crypto", inv["invoice_id"], {"reason": "tx_not_found"})  # review-only reason
    assert exc.value.status_code == 400

    sent.clear()
    res = api.call("billing_reject_crypto", inv["invoice_id"], {"reason": "duplicate"})
    assert res == {"rejected": True, "invoice_id": inv["invoice_id"], "reason": "duplicate"}
    [email] = sent
    assert (email["type"], email["invoice_amount_usd"], email["currency"]) == ("invoice_rejected", 25.0, "alpha")
    assert email["crypto_amount"] == inv["amount_to_send"]
    assert email["reason"] == "It duplicated another of your invoices, which is the one we used."
    assert email["next_step"] is None

    api.call("billing_reject_crypto", inv["invoice_id"], {"reason": "not_paid"})  # again: nothing new
    assert len(sent) == 1
    entry = _invoice_entry(api, inv["invoice_id"])
    assert (entry["status"], entry["decision_reason"]) == ("rejected", "duplicate")
    assert entry["status_message"] == "This invoice was cancelled. " + email["reason"]


def test_a_credited_invoice_cannot_be_rejected(svc, sent, api):
    inv = svc.create_crypto_invoice("alice", "usdc-base", 20)
    svc.resolve_deposit(deposit("usdc-base:0xok:0", "usdc-base", units(svc, inv["invoice_id"]), tx="0xok"))
    sent.clear()
    with pytest.raises(Exception) as exc:
        api.call("billing_reject_crypto", inv["invoice_id"], {"reason": "duplicate"})
    assert exc.value.status_code == 404
    assert sent == []


def test_an_exact_payment_needs_no_explanation(svc, sent, api):
    inv = svc.create_crypto_invoice("alice", "usdc-base", 20)
    svc.resolve_deposit(deposit("usdc-base:0xok:0", "usdc-base", units(svc, inv["invoice_id"]), tx="0xok"))
    [email] = sent
    assert email["note"] is None
    entry = _invoice_entry(api, inv["invoice_id"])
    assert (entry["status"], entry["status_message"], entry["next_step"]) == ("confirmed", None, None)


def test_paying_less_than_the_invoice_says_so(svc, sent, api):
    inv = svc.create_crypto_invoice("alice", "usdc-base", 20)
    assert svc.resolve_deposit(deposit("usdc-base:0xless:0", "usdc-base", 15_000_000, tx="0xless")) == "invoice_underpay"
    [email] = sent
    assert email["amount_usd"] == 15.0
    assert email["note"] == (
        f"15.000000 USDC on Base arrived, less than the {inv['amount_to_send']} USDC on Base the invoice asked "
        "for, so we added what arrived, at the invoice's rate."
    )
    assert _invoice_entry(api, inv["invoice_id"])["status_message"] == email["note"]


def test_paying_after_expiry_says_it_was_valued_on_arrival(svc, sent, api):
    inv = svc.create_crypto_invoice("alice", "tao", 30)
    expire(svc, inv["invoice_id"])
    svc.resolve_deposit(deposit("tao:9:1", "tao", units(svc, inv["invoice_id"]) * 1000, tx="0xlate"))
    [email] = sent
    assert "arrived after the invoice had expired" in email["note"]
    assert "arrived after the invoice had expired" in _invoice_entry(api, inv["invoice_id"])["status_message"]


def test_a_review_credited_at_another_amount_explains_it(svc, sent, api):
    inv = svc.create_crypto_invoice("alice", "usdc-base", 10)
    expire(svc, inv["invoice_id"])
    # 25 USDC is outside 50%-200% of a $10 invoice: left unmatched.
    assert svc.resolve_deposit(deposit("usdc-base:0xbig:0", "usdc-base", 25_000_000, tx="0xbig")) == "unmatched"
    api.call("billing_request_review", inv["invoice_id"], {"tx_hash": "0xbig"})
    sent.clear()
    api.call("billing_admin_resolve_review", inv["invoice_id"], {"action": "credit_deposit", "deposit_ref": "usdc-base:0xbig:0"})
    [email] = sent
    assert (email["type"], email["outcome"], email["amount_usd"]) == ("review_resolved", "credited", 25.0)
    assert email["note"].startswith("25.000000 USDC on Base arrived, more than the")
    entry = _invoice_entry(api, inv["invoice_id"])
    assert (entry["status"], entry["status_message"]) == ("confirmed", email["note"])


def test_open_review_and_expired_invoice_say_where_things_stand(svc, api):
    waiting = svc.create_crypto_invoice("alice", "usdc-base", 20)
    expire(svc, waiting["invoice_id"])
    lapsed = svc.create_crypto_invoice("alice", "usdc-base", 30)
    expire(svc, lapsed["invoice_id"])
    pending = svc.create_crypto_invoice("alice", "usdc-base", 40)
    api.call("billing_request_review", waiting["invoice_id"], {"tx_hash": "0xchk"})

    entry = _invoice_entry(api, waiting["invoice_id"])
    assert entry["status_message"].startswith("We're checking the payment you reported")
    assert entry["next_step"] is None
    entry = _invoice_entry(api, lapsed["invoice_id"])
    assert entry["status_message"] == "This invoice expired without a payment we could match to it."
    assert "verify the wallet you paid from" in entry["next_step"]
    entry = _invoice_entry(api, pending["invoice_id"])
    assert (entry["status_message"], entry["next_step"]) == (None, None)


def test_admin_reason_list(api):
    reasons = {r["code"]: r for r in api.call("billing_admin_payment_reasons")}
    assert set(reasons["tx_not_found"]["applies"]) == {"review"}
    assert set(reasons["not_paid"]["applies"]) == {"reject"}
    assert set(reasons["other"]["applies"]) == {"review", "reject"}


# --- review round 1 ----------------------------------------------------------------


def test_a_staff_confirm_of_the_invoice_amount_needs_no_note(svc, sent, api):
    inv = svc.create_crypto_invoice("alice", "usdc-base", 20)
    # 5 USDC is under 50% of the invoice: logged, unmatched.
    assert svc.resolve_deposit(deposit("usdc-base:0xfive:0", "usdc-base", 5_000_000, tx="0xfive")) == "unmatched"
    api.call("billing_confirm_crypto", inv["invoice_id"], {"tx_hash": "0xfive"})  # staff credit the full $20
    entry = _invoice_entry(api, inv["invoice_id"])
    assert entry["status"] == "confirmed"
    assert entry["status_message"] is None  # not "5 USDC arrived … so we added what arrived"


def test_a_transaction_that_already_paid_cannot_be_confirmed_again(svc, sent, api):
    first = svc.create_crypto_invoice("alice", "usdc-base", 20)
    svc.resolve_deposit(deposit("usdc-base:0xpaid:0", "usdc-base", units(svc, first["invoice_id"]), tx="0xpaid"))
    second = svc.create_crypto_invoice("bob", "usdc-base", 20)
    with pytest.raises(Exception) as exc:
        api.call("billing_confirm_crypto", second["invoice_id"], {"tx_hash": "0xPAID"})
    assert exc.value.status_code == 409 and "already credited (to alice)" in exc.value.detail
    # Bob reports Alice's transaction as his payment (alice's own report of it
    # is refused at once: it's already in her balance).
    api.routes.require_api_key = lambda *a, **k: types.SimpleNamespace(user_id="bob")
    api.call("billing_request_review", second["invoice_id"], {"tx_hash": "0xpaid"})
    with pytest.raises(Exception) as exc:
        api.call("billing_admin_resolve_review", second["invoice_id"], {"action": "confirm_invoice"})
    assert exc.value.status_code == 409 and "would pay twice" in exc.value.detail
    assert svc.get_balance("alice")["balance_credits"] == 2000  # paid once
    assert svc.get_balance("bob")["balance_credits"] == 500


def test_a_payment_credited_after_a_dismissed_review_shows_as_paid(svc, sent, api):
    inv = svc.create_crypto_invoice("alice", "tao", 30)
    expire(svc, inv["invoice_id"])
    api.call("billing_request_review", inv["invoice_id"], {"tx_hash": "0xslow"})
    api.call("billing_admin_resolve_review", inv["invoice_id"], {"action": "dismiss", "reason": "tx_not_found"})
    svc.resolve_deposit(deposit("tao:9:2", "tao", units(svc, inv["invoice_id"]) * 1000, tx="0xslow"))  # watcher catches up
    entry = _invoice_entry(api, inv["invoice_id"])
    assert entry["status"] == "confirmed"
    assert "arrived after the invoice had expired" in entry["status_message"]
    assert entry["next_step"] is None


def test_a_payment_in_another_currency_gets_no_comparison(svc, sent, api):
    inv = svc.create_crypto_invoice("alice", "tao", 30)
    expire(svc, inv["invoice_id"])
    assert svc.resolve_deposit(deposit("usdc-base:0xusdc:0", "usdc-base", 31_000_000, tx="0xusdc")) == "unmatched"
    api.call("billing_request_review", inv["invoice_id"], {"tx_hash": "0xusdc"})
    sent.clear()
    api.call("billing_admin_resolve_review", inv["invoice_id"], {"action": "credit_deposit", "deposit_ref": "usdc-base:0xusdc:0"})
    [email] = sent
    assert email["note"] is None
    assert _invoice_entry(api, inv["invoice_id"])["status_message"] is None


def test_rejecting_an_invoice_closes_its_open_review(svc, sent, api):
    inv = svc.create_crypto_invoice("alice", "usdc-base", 20)
    expire(svc, inv["invoice_id"])
    api.call("billing_request_review", inv["invoice_id"], {"tx_hash": "0xbogus"})
    sent.clear()
    api.call("billing_reject_crypto", inv["invoice_id"], {"reason": "not_linked"})
    assert [e["type"] for e in sent] == ["invoice_rejected"]
    entry = _invoice_entry(api, inv["invoice_id"])
    assert entry["review_status"] == "dismissed"
    assert entry["status_message"].startswith("This invoice was cancelled. We couldn't confirm")
    assert api.call("billing_admin_list_reviews") == []


def test_asking_again_clears_the_last_decision(svc, sent, api):
    inv = svc.create_crypto_invoice("alice", "usdc-base", 20)
    expire(svc, inv["invoice_id"])
    api.call("billing_request_review", inv["invoice_id"], {"tx_hash": "0xtypo"})
    api.call("billing_admin_resolve_review", inv["invoice_id"], {"action": "dismiss", "reason": "tx_not_found"})
    api.call("billing_request_review", inv["invoice_id"], {"tx_hash": "0xright"})
    entry = _invoice_entry(api, inv["invoice_id"])
    assert (entry["review_status"], entry["decision_reason"]) == ("open", None)


def test_ignoring_an_unmatched_deposit(svc, api):
    assert svc.resolve_deposit(deposit("usdc-base:0xours:0", "usdc-base", 500_000_000, tx="0xours")) == "unmatched"
    res = api.call("billing_admin_ignore_deposit", "usdc-base:0xours:0")
    assert (res["status"], res["matched_by"]) == ("ignored", "admin_ignored")
    with pytest.raises(Exception) as exc:
        api.call("billing_admin_ignore_deposit", "usdc-base:0xours:0")
    assert exc.value.status_code == 409
    with pytest.raises(Exception) as exc:
        api.call("billing_admin_ignore_deposit", "usdc-base:0xnope:0")
    assert exc.value.status_code == 404


def test_a_tiny_underpayment_shows_enough_decimals(svc):
    from greencompute_gateway.domain.payment_decisions import credited_note

    note = credited_note("invoice_underpay", asked="0.200000", received=0.1999997, currency="tao")
    assert note.startswith("0.199999700 TAO arrived, less than the 0.200000 TAO")


# --- review round 2 ----------------------------------------------------------------


def test_an_ignored_deposit_can_still_be_claimed_by_its_sender(svc, api):
    payer = Keypair.create_from_uri("//AliceUnknownSender")
    assert svc.resolve_deposit(deposit("tao:7:1", "tao", 50_000_000, sender=payer.ss58_address, tx="0xstray")) == "unmatched"
    api.call("billing_admin_ignore_deposit", "tao:7:1")
    credited = link(svc, "alice", payer)["credited"]
    assert [c["deposit_ref"] for c in credited] == ["tao:7:1"]
    assert svc.get_balance("alice")["balance_credits"] == 1500  # 0.05 TAO at $300


def test_the_guard_sees_a_credit_whose_deposit_row_has_no_owner_yet(svc, sent, api):
    from greencompute_persistence.orm import CryptoDepositORM

    first = svc.create_crypto_invoice("alice", "usdc-base", 20)
    svc.resolve_deposit(deposit("usdc-base:0xrace:0", "usdc-base", units(svc, first["invoice_id"]), tx="0xrace"))
    with session_scope(svc.repo.session_factory) as s:  # as the "earlier tick" branch leaves it
        s.get(CryptoDepositORM, "usdc-base:0xrace:0").user_id = None
    second = svc.create_crypto_invoice("bob", "usdc-base", 20)
    with pytest.raises(Exception) as exc:
        api.call("billing_confirm_crypto", second["invoice_id"], {"tx_hash": "0xrace"})
    assert exc.value.status_code == 409 and "(to alice)" in exc.value.detail


def test_wildcards_in_a_typed_hash_match_nothing(svc, api):
    inv = svc.create_crypto_invoice("bob", "usdc-base", 20)
    api.call("billing_confirm_crypto", inv["invoice_id"], {"tx_hash": "0xbobspayment"})  # a manual credit row
    assert svc.repo.tx_credit_owner("0xbobspayment") == "bob"
    for typed in ("%", "_", "0xbobspaymen_", "\\"):
        assert svc.repo.tx_credit_owner(typed) is None
        assert svc.repo.tx_credited_to("bob", typed) is False


def test_a_review_of_a_payment_already_added_does_not_say_we_did_not_add_it(svc, api):
    inv = svc.create_crypto_invoice("alice", "usdc-base", 20)
    expire(svc, inv["invoice_id"])
    api.call("billing_request_review", inv["invoice_id"], {"tx_hash": "0xdup"})
    api.call("billing_admin_resolve_review", inv["invoice_id"], {"action": "dismiss", "reason": "already_credited"})
    entry = _invoice_entry(api, inv["invoice_id"])
    assert entry["status_message"] == "We checked the payment you reported. That payment had already been added to your balance."

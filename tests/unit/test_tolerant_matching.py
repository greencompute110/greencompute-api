"""A payment that isn't an invoice's exact amount is still credited when it
plainly belongs to one customer: every open or recently expired invoice
within 50%-200% of it is theirs. "Credit what arrived; never refuse a payment
for its size", as the review team asked (11 USDT against a customer's two $10
invoices sat unmatched for two days). Several customers' invoices fitting
still waits for a person."""
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from greencompute_persistence import session_scope
from greencompute_persistence.orm import CryptoInvoiceORM
from greencompute_gateway.infrastructure import alerts, notify_website
from greencompute_gateway.transport import routes

from test_crypto_deposits import balance, deposit, invoice_units, status_of, svc  # noqa: F401  (fixture)


def _expire(svc, invoice_id, ago=timedelta(hours=1)):
    with session_scope(svc.repo.session_factory) as s:
        row = s.get(CryptoInvoiceORM, invoice_id)
        row.status = "expired"
        row.expires_at = datetime.now(UTC) - ago


def test_one_customers_two_invoices_and_a_round_overpayment(svc):
    first = svc.create_crypto_invoice("alice", "usdc-base", 10)
    second = svc.create_crypto_invoice("alice", "usdc-base", 10)
    assert svc.resolve_deposit(deposit("usdc-base:0x11:0", amount_raw=11_000_000)) == "invoice_overpay"
    assert balance(svc, "alice") == 1100  # all 11 USDT, not just the $10 invoiced
    assert status_of(svc, first["invoice_id"]) == "confirmed"  # the oldest
    assert status_of(svc, second["invoice_id"]) == "pending"


def test_an_underpayment_credits_what_arrived(svc):
    inv = svc.create_crypto_invoice("alice", "tao", 60)  # ~0.2 TAO at $300
    sent = round(float(inv["amount_crypto"]) * 0.91, 9)  # a transfer_all that fell short
    assert svc.resolve_deposit(deposit("tao:0xa:0", currency="tao", amount_raw=int(sent * 1e9))) == "invoice_underpay"
    assert balance(svc, "alice") == round(60 * 0.91 * 100)  # $54.60 at the invoice's rate
    assert status_of(svc, inv["invoice_id"]) == "confirmed"


def test_two_customers_could_be_paying_so_it_waits_for_a_person(svc):
    svc.create_crypto_invoice("alice", "usdc-base", 10)
    svc.create_crypto_invoice("bob", "usdc-base", 12)
    assert svc.resolve_deposit(deposit("usdc-base:0x12:0", amount_raw=11_000_000)) == "unmatched"
    assert (balance(svc, "alice"), balance(svc, "bob")) == (0, 0)


@pytest.mark.parametrize("raw", [4_900_000, 20_100_000])  # under 50% / over 200% of $10
def test_far_from_any_invoice_is_not_guessed(svc, raw):
    svc.create_crypto_invoice("alice", "usdc-base", 10)
    assert svc.resolve_deposit(deposit("usdc-base:0x13:0", amount_raw=raw)) == "unmatched"
    assert balance(svc, "alice") == 0


def test_a_late_off_amount_payment_is_credited_at_its_value_on_arrival(svc):
    inv = svc.create_crypto_invoice("alice", "tao", 60)
    _expire(svc, inv["invoice_id"])
    assert svc.resolve_deposit(deposit("tao:0xb:0", currency="tao", amount_raw=150_000_000)) == "invoice_late"
    assert balance(svc, "alice") == 4500  # 0.15 TAO at today's $300
    assert status_of(svc, inv["invoice_id"]) == "confirmed"


def test_an_expired_invoice_too_old_is_not_matched(svc):
    inv = svc.create_crypto_invoice("alice", "tao", 60)
    _expire(svc, inv["invoice_id"], ago=timedelta(days=3))
    assert svc.resolve_deposit(deposit("tao:0xc:0", currency="tao", amount_raw=150_000_000)) == "unmatched"


def test_an_exact_amount_still_wins(svc):
    mine = svc.create_crypto_invoice("alice", "usdc-base", 10)
    svc.create_crypto_invoice("bob", "usdc-base", 10)
    raw = invoice_units(svc, mine["invoice_id"])
    assert svc.resolve_deposit(deposit("usdc-base:0x14:0", amount_raw=raw)) == "invoice"
    assert (balance(svc, "alice"), balance(svc, "bob")) == (1000, 0)


def test_one_customers_other_open_invoice_in_another_coin_doesnt_matter(svc):
    svc.create_crypto_invoice("alice", "usdc-base", 10)
    svc.create_crypto_invoice("bob", "tao", 10)  # a different coin: not a candidate
    assert svc.resolve_deposit(deposit("usdc-base:0x15:0", amount_raw=10_500_000)) == "invoice_overpay"
    assert balance(svc, "alice") == 1050


# --- one transaction, one review -----------------------------------------------


@pytest.fixture
def reviews(svc, monkeypatch):
    monkeypatch.setattr(routes, "_get_billing", lambda: svc)
    sent = []
    monkeypatch.setattr(alerts, "send_payment_alert", lambda text, **kw: sent.append((text, kw)))
    monkeypatch.setattr(svc, "email_review_received", lambda invoice_id: None)
    return sent


def test_the_same_transaction_cant_be_filed_on_two_invoices(svc, reviews):
    a = svc.create_crypto_invoice("alice", "usdc-base", 10)
    b = svc.create_crypto_invoice("alice", "usdc-base", 10)
    routes._request_review(a["invoice_id"], "alice", "0xFACF", None)
    with pytest.raises(HTTPException) as err:
        routes._request_review(b["invoice_id"], "alice", "0xfacf", None)
    assert err.value.status_code == 409 and "already asked us to check" in err.value.detail
    # re-sending on the same invoice (e.g. to add a note) is fine
    routes._request_review(a["invoice_id"], "alice", "0xFACF", "sent from Binance")


def test_a_transaction_already_credited_isnt_reviewed_again(svc, reviews):
    svc.create_crypto_invoice("alice", "usdc-base", 10)
    svc.resolve_deposit(deposit("usdc-base:0x16:0", amount_raw=11_000_000, tx="0xpaid"))
    later = svc.create_crypto_invoice("alice", "usdc-base", 10)
    with pytest.raises(HTTPException) as err:
        routes._request_review(later["invoice_id"], "alice", "0xPAID", None)
    assert err.value.status_code == 409 and "already been added" in err.value.detail


def test_a_review_request_doesnt_also_email_a_plain_alert(svc, reviews):
    inv = svc.create_crypto_invoice("alice", "usdc-base", 10)
    routes._request_review(inv["invoice_id"], "alice", "0xabc", None)
    assert reviews and reviews[0][1] == {"email_support": False}


# --- alerts reach a person -----------------------------------------------------


def test_payment_alerts_are_emailed_to_support(monkeypatch):
    got = []
    monkeypatch.setattr(notify_website, "notify_support", got.append)
    monkeypatch.setattr(alerts, "_requests", lambda text: [])
    alerts.send_payment_alert("Unmatched payment: 11 USDT-ETH")
    alerts.send_payment_alert("Payment review requested", email_support=False)
    assert got == ["Unmatched payment: 11 USDT-ETH"]


def test_the_support_email_is_posted_to_the_website(monkeypatch):
    posted = []
    monkeypatch.setenv("GREENCOMPUTE_PROVISION_SECRET", "s3cret")
    monkeypatch.setenv("GREENCOMPUTE_PAYMENT_EMAILS_ENABLED", "true")
    monkeypatch.setattr(notify_website, "_post", lambda url, secret, data: posted.append((url, secret, data)) or 200)

    class Now:
        def __init__(self, target, args, **kw):
            self.target, self.args = target, args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(notify_website.threading, "Thread", Now)
    notify_website.notify_support("Unmatched payment: 11 USDT-ETH")
    (url, secret, data), = posted
    assert secret == "s3cret" and b'"type": "ops_alert"' in data and b"11 USDT-ETH" in data

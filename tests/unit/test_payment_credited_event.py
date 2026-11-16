"""`payment.credited`: one event per payment added to a balance, written in
the transaction that adds it, for every way a payment arrives: an invoice, a
verified wallet, a card, staff. Nothing for a payment already credited."""
import pytest

from greencompute_gateway.application import webhooks
from greencompute_gateway.application.billing_service import BillingService
from greencompute_gateway.infrastructure.billing_repository import BillingRepository
from greencompute_persistence import session_scope
from greencompute_persistence.account_events import EVENT_TYPES
from greencompute_persistence.orm import AccountEventORM, CryptoInvoiceORM, UserORM
from greencompute_protocol import StripeSession

USDC = "0x" + "ab" * 20


@pytest.fixture
def svc(monkeypatch):
    monkeypatch.setenv("BILLING_DEPOSIT_USDC_BASE", USDC)
    repo = BillingRepository(database_url="sqlite:///:memory:", bootstrap=True)
    with session_scope(repo.session_factory) as s:
        s.add(UserORM(user_id="u1", username="u1", email="u1@x.io", balance_credits=0, profile_metadata={}))
    return BillingService(repo)


def events(svc):
    with session_scope(svc.repo.session_factory) as s:
        return [(e.type, dict(e.data)) for e in s.query(AccountEventORM).order_by(AccountEventORM.event_id)]


def test_it_is_a_documented_event_type_webhooks_can_ask_for():
    assert "payment.credited" in EVENT_TYPES
    assert webhooks._types(["payment.credited"]) == ["payment.credited"]


def test_an_invoice_paid_on_chain(svc):
    inv = svc.create_crypto_invoice("u1", "usdc-base", 20)
    with session_scope(svc.repo.session_factory) as s:
        units = s.get(CryptoInvoiceORM, inv["invoice_id"]).amount_units
    dep = {"deposit_ref": "usdc-base:0xa:0", "currency": "usdc-base", "tx_hash": "0xa", "sender": "0x" + "99" * 20,
           "deposit_address": USDC, "amount_raw": units, "decimals": 6, "block_number": 1, "usd_price": 1.0}
    assert svc.resolve_deposit(dep) == "invoice"
    assert svc.resolve_deposit(dep) == "seen"
    [(kind, data)] = events(svc)
    assert kind == "payment.credited"
    assert (data["amount_usd"], data["balance_usd"], data["method"], data["currency"], data["invoice_id"],
            data["tx_hash"]) == (20.0, 20.0, "crypto", "usdc-base", inv["invoice_id"], "0xa")


def test_a_card_payment_and_a_staff_credit(svc):
    svc.repo.create_stripe_session(StripeSession(user_id="u1", stripe_session_id="cs_1", amount_usd=10,
                                                 amount_cents=1000))
    assert svc.repo.complete_and_credit_stripe_session("cs_1")["credited"]
    assert svc.repo.complete_and_credit_stripe_session("cs_1")["already_credited"]
    svc.repo.credit_user("u1", 500, kind="topup", description="Manual admin credit")
    svc.repo.credit_user("u1", 100, kind="bonus")  # not a payment
    assert [(d["method"], d["amount_cents"], d["balance_usd"]) for _k, d in events(svc)] == [
        ("card", 1000, 10.0), ("manual", 500, 15.0)]

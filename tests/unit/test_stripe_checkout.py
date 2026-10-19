"""Card top-ups: the checkout session is created without the
`payment_method_types` parameter newer Stripe API versions reject, and a
Stripe failure reaches the dashboard as a 503 with a message to show."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from greencompute_gateway.infrastructure import stripe_client


class FakeSession:
    calls: list[dict] = []
    fail: Exception | None = None

    @classmethod
    def create(cls, **kwargs):
        cls.calls.append(kwargs)
        if cls.fail:
            raise cls.fail
        return SimpleNamespace(id="cs_test_1", url="https://checkout.stripe.com/c/pay/cs_test_1")


@pytest.fixture
def fake_stripe(monkeypatch):
    FakeSession.calls, FakeSession.fail = [], None
    monkeypatch.setattr(stripe_client, "_get_stripe", lambda: SimpleNamespace(checkout=SimpleNamespace(Session=FakeSession)))
    return FakeSession


def test_checkout_leaves_payment_methods_to_the_dashboard(fake_stripe):
    assert stripe_client.create_checkout_session(2500, "u1") == ("cs_test_1", "https://checkout.stripe.com/c/pay/cs_test_1")
    (call,) = fake_stripe.calls
    assert "payment_method_types" not in call
    assert call["line_items"][0]["price_data"]["unit_amount"] == 2500 and call["metadata"] == {"user_id": "u1"}


def test_a_stripe_failure_is_a_503_with_a_message(fake_stripe, monkeypatch):
    from greencompute_gateway.transport import routes

    fake_stripe.fail = Exception("The `payment_method_types` parameter is no longer supported")
    monkeypatch.setattr(routes, "require_api_key", lambda *a: SimpleNamespace(user_id="u1"))

    class Billing:
        def create_stripe_topup(self, user_id, amount_usd):
            return stripe_client.create_checkout_session(int(amount_usd * 100), user_id)

    monkeypatch.setattr(routes, "_get_billing", lambda: Billing())
    with pytest.raises(HTTPException) as err:
        routes.billing_topup_stripe({"amount_usd": 25}, authorization="Bearer k")
    assert err.value.status_code == 503 and "Card payments are unavailable" in err.value.detail

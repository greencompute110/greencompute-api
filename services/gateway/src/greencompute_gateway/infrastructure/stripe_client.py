"""Stripe integration — checkout session creation and webhook verification."""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_SUCCESS_URL = os.environ.get("STRIPE_SUCCESS_URL", "https://green-compute.com/billing?status=success")
STRIPE_CANCEL_URL = os.environ.get("STRIPE_CANCEL_URL", "https://green-compute.com/billing?status=cancelled")


class StripeNotConfiguredError(RuntimeError):
    """Raised when Stripe is called but the API key isn't set.

    The gateway handler catches this and surfaces a 503 rather than a 500
    so the missing-configuration cause is obvious in logs and in the UI.
    """


def _get_stripe():
    """Lazy import + configuration guard. Only call if you intend to hit Stripe."""
    try:
        import stripe
    except ImportError as exc:
        raise RuntimeError(
            "stripe package not installed — ensure docker-compose pip install includes 'stripe'"
        ) from exc
    if not STRIPE_SECRET_KEY:
        raise StripeNotConfiguredError(
            "STRIPE_SECRET_KEY is not set. Add it to infra/production/.env and "
            "restart the gateway container."
        )
    stripe.api_key = STRIPE_SECRET_KEY
    return stripe


def create_checkout_session(amount_cents: int, user_id: str) -> tuple[str, str]:
    """Create a Stripe Checkout Session. Returns (session_id, checkout_url)."""
    stripe = _get_stripe()
    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": "usd",
                "unit_amount": amount_cents,
                "product_data": {
                    "name": "GreenCompute Credits",
                    "description": f"${amount_cents / 100:.2f} USD top-up",
                },
            },
            "quantity": 1,
        }],
        mode="payment",
        success_url=STRIPE_SUCCESS_URL,
        cancel_url=STRIPE_CANCEL_URL,
        metadata={"user_id": user_id},
    )
    return session.id, session.url


def verify_webhook_signature(payload: bytes, sig_header: str) -> dict:
    """Verify a Stripe webhook signature and return the event as a PLAIN dict.

    `stripe.Webhook.construct_event` returns a `stripe.Event` StripeObject.
    Across stripe-python versions that object does NOT reliably expose dict's
    `.get()` (attribute access routes through __getattr__ and raises), which
    made the webhook handler 500 on every event — so no Stripe payment ever
    credited. construct_event still does the signature validation we need;
    we then hand back `json.loads(payload)` (the same authenticated bytes) so
    the caller can use ordinary dict access safely.
    """
    import json

    stripe = _get_stripe()
    # Raises stripe.error.SignatureVerificationError on a bad signature.
    stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    return json.loads(payload.decode() if isinstance(payload, bytes) else payload)


def retrieve_checkout_session(stripe_session_id: str) -> dict:
    """Fetch a Checkout Session from Stripe and return key fields as a plain
    dict. Used by the reconcile job to confirm a session was actually PAID
    before retroactively crediting (so abandoned checkouts aren't credited).
    """
    stripe = _get_stripe()
    s = stripe.checkout.Session.retrieve(stripe_session_id)
    # Attribute access is the documented StripeObject API and is safe (it was
    # `.get()` specifically that misbehaved across versions).
    return {
        "id": s.id,
        "status": getattr(s, "status", None),            # "complete" when finished
        "payment_status": getattr(s, "payment_status", None),  # "paid" when captured
        "amount_total": getattr(s, "amount_total", None),
        # PaymentIntent id ("pi_..."), the join key for refund/dispute webhooks
        # back to this top-up. NULL until the customer pays. (BILL-M1 Part B)
        "payment_intent": _payment_intent_id(getattr(s, "payment_intent", None)),
    }


def _payment_intent_id(pi) -> str | None:
    """Normalize a Stripe `payment_intent` reference to its string id.

    Depending on the API call / expansion, `payment_intent` may be a bare id
    string, a dict, or a StripeObject. We only ever want the id string.
    """
    if pi is None:
        return None
    if isinstance(pi, str):
        return pi or None
    if isinstance(pi, dict):
        val = pi.get("id")
        return val or None
    val = getattr(pi, "id", None)
    return val or None

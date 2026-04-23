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
    """Verify and parse a Stripe webhook event."""
    stripe = _get_stripe()
    event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    return event

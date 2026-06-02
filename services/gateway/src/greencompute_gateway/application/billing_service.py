from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

from greencompute_protocol import CryptoInvoice, LedgerEntry, StripeSession
from greencompute_gateway.infrastructure.billing_repository import BillingRepository, InsufficientBalanceError

log = logging.getLogger(__name__)

# Bonus rates keyed by the base currency (chain suffix stripped for lookup).
# Stablecoins (USDT/USDC) are par with USD and get no bonus — we already
# don't pay any FX/spread on them. Crypto rails (TAO/Alpha) get a bonus to
# offset their volatility AND to push demand toward subnet-aligned rails.
BONUS_RATES: dict[str, float] = {
    "stripe": 0.00,
    "usdt": 0.00,
    "usdc": 0.00,
    "tao": 0.10,
    "alpha": 0.10,
}


def _deposit_address_for(currency: str) -> str:
    """Resolve a deposit address for a currency identifier.

    Accepts both chain-qualified codes (``usdt-eth``, ``usdt-base``,
    ``usdc-eth``, ``usdc-base``) and plain base codes (``usdt``, ``usdc``,
    ``tao``, ``alpha``). For stablecoins, the chain-specific env var is
    preferred; if unset, falls back to the legacy single-address env var
    so older deployments keep working.
    """
    c = currency.lower()
    if c == "usdt-eth":
        return os.environ.get("BILLING_DEPOSIT_USDT_ETH") or os.environ.get("BILLING_DEPOSIT_USDT", "")
    if c == "usdt-base":
        return os.environ.get("BILLING_DEPOSIT_USDT_BASE") or os.environ.get("BILLING_DEPOSIT_USDT", "")
    if c == "usdc-eth":
        return os.environ.get("BILLING_DEPOSIT_USDC_ETH") or os.environ.get("BILLING_DEPOSIT_USDC", "")
    if c == "usdc-base":
        return os.environ.get("BILLING_DEPOSIT_USDC_BASE") or os.environ.get("BILLING_DEPOSIT_USDC", "")
    if c == "usdt":
        return os.environ.get("BILLING_DEPOSIT_USDT", "")
    if c == "usdc":
        return os.environ.get("BILLING_DEPOSIT_USDC", "")
    if c == "tao":
        return os.environ.get("BILLING_DEPOSIT_TAO", "")
    if c == "alpha":
        return os.environ.get("BILLING_DEPOSIT_ALPHA", "")
    return ""


def _base_currency(currency: str) -> str:
    """Strip chain suffix for bonus-rate lookups and display."""
    c = currency.lower()
    if "-" in c:
        return c.split("-", 1)[0]
    return c


class BillingService:
    def __init__(self, billing_repo: BillingRepository) -> None:
        self.repo = billing_repo

    # --- Balance ---

    def get_balance(self, user_id: str) -> dict:
        credits = self.repo.get_balance(user_id)
        return {
            "balance_credits": credits,
            "balance_usd": round(credits / 100.0, 2),
        }

    def list_ledger(self, user_id: str, limit: int = 50, offset: int = 0) -> list[LedgerEntry]:
        return self.repo.list_ledger(user_id, limit=limit, offset=offset)

    def check_balance(self, user_id: str, required_cents: int) -> bool:
        return self.repo.get_balance(user_id) >= required_cents

    # --- Stripe top-up ---

    def create_stripe_topup(self, user_id: str, amount_usd: float) -> dict:
        """Create a Stripe checkout session. Returns the checkout URL."""
        from greencompute_gateway.infrastructure.stripe_client import create_checkout_session

        amount_cents = int(round(amount_usd * 100))
        stripe_session_id, checkout_url = create_checkout_session(
            amount_cents=amount_cents,
            user_id=user_id,
        )
        ss = StripeSession(
            user_id=user_id,
            stripe_session_id=stripe_session_id,
            amount_usd=amount_usd,
            amount_cents=amount_cents,
        )
        self.repo.create_stripe_session(ss)
        return {
            "session_id": ss.session_id,
            "stripe_session_id": stripe_session_id,
            "checkout_url": checkout_url,
            "amount_usd": amount_usd,
            "amount_cents": amount_cents,
        }

    def confirm_stripe_payment(
        self,
        stripe_session_id: str,
        payment_intent_id: str | None = None,
    ) -> dict | None:
        """Idempotent + atomic: credit the user for a completed Stripe session.

        Delegates the status-flip + credit to the single-transaction
        `complete_and_credit_stripe_session` (SELECT..FOR UPDATE + UNIQUE
        deposit_ref idempotency), so concurrent webhook deliveries / a webhook
        racing the reconcile job credit exactly once. We only fire the ops
        notification on a FRESH credit so retries/races don't re-notify.

        ``payment_intent_id`` (BILL-M1 Part B): the completed Stripe session
        carries its PaymentIntent id ("pi_..."), which we record on the session
        row so a later `charge.refunded` / `charge.dispute.created` webhook can
        map back to this top-up. It doesn't exist at session-create time, so the
        completion path (webhook / reconcile) is where we learn it. Recorded
        even on a re-delivery (the repo write is idempotent on NULL) so an old
        session that completed before this column existed still gets stamped.
        """
        # Look up first so a non-existent session returns None (the webhook /
        # reconcile callers distinguish "unknown session" from "credited").
        existing = self.repo.get_stripe_session_by_stripe_id(stripe_session_id)
        if existing is None:
            return None

        # Record the payment_intent join key BEFORE crediting so a refund that
        # races in immediately can still resolve the session. Idempotent: the
        # repo only writes when the column is currently NULL/blank.
        if payment_intent_id:
            try:
                self.repo.set_stripe_payment_intent(stripe_session_id, payment_intent_id)
            except Exception:
                log.exception(
                    "failed to record payment_intent for stripe session %s",
                    stripe_session_id,
                )

        result = self.repo.complete_and_credit_stripe_session(stripe_session_id)
        if result is None:
            return None
        if result.get("credited"):
            amount_cents = int(result.get("amount_cents") or 0)
            log.info(
                "Stripe payment credited: user=%s amount=%d",
                existing.user_id,
                amount_cents,
            )
            try:
                from greencompute_gateway.infrastructure.notifications import notify_big_topup

                notify_big_topup(
                    user_id=existing.user_id,
                    amount_usd=existing.amount_usd,
                    source="stripe",
                    reference=existing.session_id,
                )
            except Exception:
                log.exception(
                    "ops notification (stripe) failed for session %s", existing.session_id
                )
        return result

    def reconcile_pending_stripe_sessions(self, *, dry_run: bool = True) -> dict:
        """Recover top-ups that should have been credited by the webhook but
        weren't (e.g. while the webhook handler was 500-ing).

        SAFE: for each `pending` session we ask Stripe whether it was actually
        PAID before crediting — abandoned checkouts (user never paid) stay
        pending and are NOT credited. Idempotent via confirm_stripe_payment.

        Returns a summary. With dry_run=True (default) nothing is credited;
        it just reports what WOULD be credited.
        """
        from greencompute_gateway.infrastructure.stripe_client import (
            retrieve_checkout_session,
        )

        pending = self.repo.list_pending_stripe_sessions()
        summary = {
            "checked": len(pending),
            "paid_credited": [],
            "paid_would_credit": [],
            "unpaid_skipped": [],
            "errors": [],
            "dry_run": dry_run,
        }
        for ss in pending:
            try:
                remote = retrieve_checkout_session(ss.stripe_session_id)
            except Exception as exc:  # noqa: BLE001
                summary["errors"].append({"session": ss.stripe_session_id, "error": str(exc)})
                continue
            is_paid = (remote.get("payment_status") == "paid") or (remote.get("status") == "complete")
            entry = {
                "session": ss.stripe_session_id,
                "user_id": ss.user_id,
                "amount_cents": ss.amount_cents,
                "payment_status": remote.get("payment_status"),
            }
            if not is_paid:
                summary["unpaid_skipped"].append(entry)
                continue
            if dry_run:
                summary["paid_would_credit"].append(entry)
            else:
                # Pass the remote payment_intent so the reconcile path also
                # backfills the refund/dispute join key on the session row.
                result = self.confirm_stripe_payment(
                    ss.stripe_session_id,
                    payment_intent_id=remote.get("payment_intent"),
                )
                entry["result"] = result
                summary["paid_credited"].append(entry)
        return summary

    def reverse_stripe_topup_for_event(
        self,
        *,
        event_id: str,
        payment_intent_id: str | None,
        amount_cents: int,
        reason: str,
    ) -> dict | None:
        """Reverse a Stripe top-up in response to a `charge.refunded` /
        `charge.dispute.created` webhook. (BILL-M1 Part B)

        Resolves the originating top-up via the session's recorded
        ``payment_intent_id`` (the refund/dispute event carries the
        payment_intent, NOT our checkout session id). If the session can't be
        resolved — e.g. a HISTORICAL session whose payment_intent_id is NULL
        because it predates the column — we log + SKIP (return None) for manual
        reconcile rather than guessing which user to debit.

        On a resolved match, books an idempotent debit via
        ``reverse_stripe_topup`` keyed on ``deposit_ref='stripe-refund:{event_id}'``
        so a re-delivered event reverses at most once. The balance MAY go
        negative (admin claw-back semantics, BILL-M1-A).
        """
        pi = (payment_intent_id or "").strip()
        if not pi:
            log.warning(
                "Stripe %s event %s has no payment_intent — skipping (manual reconcile)",
                reason, event_id,
            )
            return None
        session = self.repo.get_stripe_session_by_payment_intent(pi)
        if session is None:
            log.warning(
                "Stripe %s event %s: payment_intent %s does not map to any known "
                "top-up session (historical NULL payment_intent_id?) — skipping "
                "for manual reconcile",
                reason, event_id, pi,
            )
            return None
        amount_cents = int(amount_cents)
        if amount_cents <= 0:
            log.warning(
                "Stripe %s event %s for session %s: non-positive amount %d — skipping",
                reason, event_id, session.session_id, amount_cents,
            )
            return None
        result = self.repo.reverse_stripe_topup(
            user_id=session.user_id,
            amount_cents=amount_cents,
            event_id=event_id,
            reference_id=session.session_id,
            description=f"Stripe {reason} reversal (${amount_cents / 100:.2f})",
            kind="chargeback",
        )
        if result.get("reversed"):
            log.info(
                "Stripe %s reversed: user=%s session=%s amount=%d event=%s",
                reason, session.user_id, session.session_id, amount_cents, event_id,
            )
        else:
            log.info(
                "Stripe %s event %s already reversed (idempotent no-op)",
                reason, event_id,
            )
        return result

    # --- Crypto top-up ---

    def create_crypto_invoice(self, user_id: str, currency: str, amount_usd: float) -> dict:
        """Create a crypto deposit invoice.

        `currency` may be a bare code (``usdt``, ``usdc``, ``tao``, ``alpha``)
        or a chain-qualified stablecoin code (``usdt-eth``, ``usdt-base``,
        ``usdc-eth``, ``usdc-base``). The chain suffix is preserved on the
        invoice so admins can tell which network the sender used; bonus
        rates are resolved against the base currency.
        """
        currency = currency.lower()
        base = _base_currency(currency)
        bonus_pct = BONUS_RATES.get(base, 0.0)
        base_cents = int(round(amount_usd * 100))
        bonus_cents = int(round(base_cents * bonus_pct))
        total_credits = base_cents + bonus_cents

        # Stablecoins settle 1:1 USD. TAO/Alpha use a live price feed.
        if base in ("usdt", "usdc"):
            amount_crypto = amount_usd
        else:
            from greencompute_gateway.infrastructure.price_feed import get_price
            price = get_price(base)
            amount_crypto = round(amount_usd / price, 6) if price > 0 else 0.0

        deposit_address = _deposit_address_for(currency)
        invoice = CryptoInvoice(
            user_id=user_id,
            currency=currency,
            amount_crypto=amount_crypto,
            amount_usd=amount_usd,
            bonus_pct=bonus_pct,
            total_credits=total_credits,
            deposit_address=deposit_address,
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
        )
        self.repo.create_crypto_invoice(invoice)
        return {
            "invoice_id": invoice.invoice_id,
            "currency": currency,
            "amount_crypto": amount_crypto,
            "amount_usd": amount_usd,
            "bonus_pct": bonus_pct,
            "total_credits": total_credits,
            "deposit_address": deposit_address,
            "expires_at": invoice.expires_at.isoformat(),
        }

    def confirm_crypto_deposit(self, invoice_id: str, tx_hash: str) -> dict | None:
        """Admin confirms a crypto deposit. Credits user with bonus.

        Atomic — the repo runs the invoice flip and the ledger insert in a
        single session, so we can't end up in a "confirmed-without-credit"
        stuck state if anything in between fails. Also idempotent: a retry
        once a ledger entry already exists is a no-op.
        """
        invoice = self.repo.get_crypto_invoice(invoice_id)
        if invoice is None:
            return None
        description = (
            f"Crypto deposit {invoice.currency.upper()} "
            f"${invoice.amount_usd:.2f} "
            f"(+{int(invoice.bonus_pct * 100)}% bonus)"
        )
        # Route the manual admin confirm through the same per-transfer
        # idempotency key as the auto-watcher. An admin pasting the same
        # tx_hash into two different invoices now credits only the first —
        # the UNIQUE deposit_ref blocks the second. Qualify with currency so
        # a hash can't collide across chains, and prefix "manual:" so it
        # never clashes with an auto-watcher ref for the same transfer
        # (worst case the manual confirm wins; the watcher then sees the
        # invoice already credited and no-ops).
        deposit_ref = (
            f"manual:{invoice.currency}:{tx_hash}" if tx_hash else None
        )
        result = self.repo.confirm_and_credit_invoice(
            invoice_id=invoice_id,
            tx_hash=tx_hash,
            description=description,
            deposit_ref=deposit_ref,
        )
        if result is None:
            return None
        if result.get("credited"):
            log.info(
                "Crypto deposit confirmed: user=%s invoice=%s credits=%d",
                invoice.user_id,
                invoice_id,
                invoice.total_credits,
            )
            try:
                from greencompute_gateway.infrastructure.notifications import notify_big_topup

                notify_big_topup(
                    user_id=invoice.user_id,
                    amount_usd=invoice.amount_usd,
                    source=f"crypto/{invoice.currency}",
                    reference=invoice_id,
                )
            except Exception:
                log.exception("ops notification (crypto) failed for invoice %s", invoice_id)
        return result

    # --- Usage deduction ---

    def deduct_usage(self, user_id: str, deployment_id: str, amount_cents: int) -> LedgerEntry:
        return self.repo.debit_user(
            user_id=user_id,
            amount_cents=amount_cents,
            kind="usage",
            reference_id=deployment_id,
            description=f"GPU usage for deployment {deployment_id[:8]}",
        )


# Singleton — lazily initialized
_billing_service: BillingService | None = None


def get_billing_service() -> BillingService:
    global _billing_service
    if _billing_service is None:
        repo = BillingRepository()
        _billing_service = BillingService(repo)
    return _billing_service

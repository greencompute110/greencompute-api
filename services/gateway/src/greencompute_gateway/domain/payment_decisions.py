"""Why a crypto payment was or wasn't added, in words a customer can act on.

Staff close a payment review without crediting it, or reject an invoice, only
with one of REASONS; the customer then gets that reason and what to do next by
email, on the Billing page and in GET /platform/billing/crypto/invoices. A
payment credited at a different amount from its invoice (less, more, or after
the invoice expired) is explained the same way. The sentences live here, so
the email, the page and the API never disagree.
"""
from __future__ import annotations

from dataclasses import dataclass

SUPPORT_EMAIL = "support@green-compute.com"


@dataclass(frozen=True)
class Reason:
    label: str  # for staff, in the admin picker
    message: str  # for the customer: why. {network} and {currency} are filled in
    next_step: str | None  # for the customer: what to do now
    applies: tuple[str, ...]  # "review" (closing a review), "reject" (rejecting an invoice)


REASONS: dict[str, Reason] = {
    "tx_not_found": Reason(
        "Transaction not found on the chain",
        "We couldn't find that transaction on the {network} network.",
        "Check the transaction hash in your wallet or on a block explorer, and that you sent it on {network}, "
        "then ask us to check it again from the Billing page.",
        ("review",),
    ),
    "wrong_address": Reason(
        "It didn't go to our deposit address",
        "That transaction didn't send {currency} to our deposit address, so the payment never reached us.",
        "If it went to another address or network, the wallet or exchange you sent it from may be able to help. "
        "Our deposit addresses are on the Billing page.",
        ("review",),
    ),
    "wrong_subnet": Reason(
        "Alpha of another subnet",
        "That transaction moved alpha of a different subnet. We can only add subnet 110 alpha.",
        f"Write to {SUPPORT_EMAIL} and we'll work out with you what to do with it.",
        ("review",),
    ),
    "already_credited": Reason(
        "Already added to their balance",
        "That payment had already been added to your balance.",
        "Nothing to do: you'll find it in your billing history.",
        ("review", "reject"),
    ),
    "not_linked": Reason(
        "Can't tie the payment to this account",
        "We couldn't confirm that the payment came from you.",
        "Verify the wallet you sent it from on the Billing page: payments it sent in the last 30 days are then "
        f"added automatically. Or send proof of the payment, such as your exchange's withdrawal record, to {SUPPORT_EMAIL}.",
        ("review", "reject"),
    ),
    "not_paid": Reason(
        "No payment arrived for it",
        "No payment arrived for it.",
        "Create a new invoice when you're ready to pay. If you did pay it, ask us to check the payment from the "
        "Billing page.",
        ("reject",),
    ),
    "duplicate": Reason(
        "Duplicate of another invoice",
        "It duplicated another of your invoices, which is the one we used.",
        None,
        ("reject",),
    ),
    "other": Reason(
        "Other (write the reason in the note)",
        "",  # the staff note is the message
        f"Write to {SUPPORT_EMAIL} if you have questions.",
        ("review", "reject"),
    ),
}


class DecisionError(ValueError):
    """A review closed or invoice rejected without a usable reason."""


def check_reason(code: str | None, note: str | None, action: str) -> tuple[str, str | None]:
    """Validate a staff decision: (reason code, cleaned note)."""
    code = (code or "").strip()
    note = (note or "").strip() or None
    reason = REASONS.get(code)
    if reason is None or action not in reason.applies:
        allowed = ", ".join(c for c, r in REASONS.items() if action in r.applies)
        raise DecisionError(f"reason must be one of: {allowed}")
    if code == "other" and not note:
        raise DecisionError("write the reason in note when reason is 'other'")
    if note and len(note) > 1000:
        raise DecisionError("note is too long (1000 characters at most)")
    return code, note


def reason_list() -> list[dict]:
    """For the admin picker."""
    return [{"code": c, "label": r.label, "applies": list(r.applies)} for c, r in REASONS.items()]


def network_name(currency: str) -> str:
    c = (currency or "").lower()
    if c in ("tao", "alpha"):
        return "Bittensor"
    if c.endswith("-base"):
        return "Base"
    if c.endswith("-eth"):
        return "Ethereum"
    return c.upper()


def currency_label(currency: str) -> str:
    """"USDT on Ethereum", "TAO", "Alpha": as the website writes them."""
    token, _, net = (currency or "").upper().partition("-")
    if token == "ALPHA":
        return "Alpha"
    network = {"BASE": "Base", "ETH": "Ethereum"}.get(net, net)
    return f"{token} on {network}" if network else token


def explain_reason(code: str | None, note: str | None, currency: str) -> tuple[str, str | None]:
    """(why, what to do next) for a staff decision. A decision from before
    reasons existed (no code) gets a general next step."""
    reason = REASONS.get(code or "")
    if reason is None:
        why = note or ""
        return why, f"If you think we got this wrong, write to {SUPPORT_EMAIL} with the transaction hash."
    fill = {"network": network_name(currency), "currency": currency_label(currency)}
    if code == "other":
        why = note or ""
    else:
        why = reason.message.format(**fill)
        if note:
            why = f"{why} Note from our team: {note}"
    return why, reason.next_step.format(**fill) if reason.next_step else None


def _tokens(amount: float | str, currency: str) -> str:
    text = amount if isinstance(amount, str) else f"{amount:.6f}"
    return f"{text} {currency_label(currency)}"


def credited_note(
    how: str | None,
    *,
    asked: str,
    received: float | str,
    currency: str,
    paid_currency: str | None = None,
) -> str | None:
    """Why a credit differs from the invoice, or None when it doesn't.
    `asked` and `received` are token amounts; `how` is how the deposit was
    matched (crypto_deposits.matched_by). A payment in another currency than
    the invoice's (`paid_currency`) can't be compared, so it gets no note."""
    if paid_currency and paid_currency != currency:
        return None
    try:
        got, want = float(received), float(asked)
    except (TypeError, ValueError):
        return None
    # Six decimals would print 0.199999999 TAO as 0.200000: show enough to
    # see the difference.
    received_text = f"{got:.9f}" if f"{got:.6f}" == f"{want:.6f}" and got != want else f"{got:.6f}"
    if how == "invoice_underpay":
        return (
            f"{_tokens(received_text, currency)} arrived, less than the {_tokens(asked, currency)} the invoice asked "
            "for, so we added what arrived, at the invoice's rate."
        )
    if how == "invoice_overpay":
        return (
            f"{_tokens(received_text, currency)} arrived, more than the {_tokens(asked, currency)} the invoice asked "
            "for, so we added all of it, at the invoice's rate."
        )
    if how == "invoice_late":
        return (
            f"{_tokens(received_text, currency)} arrived after the invoice had expired, so we added its value when it "
            "arrived rather than the invoice's amount."
        )
    # "admin" credited what arrived; "admin_invoice" (a staff confirm) the
    # invoice's own amount, so it needs no note.
    if how in ("admin", "wallet", "wallet_link"):
        if abs(got - want) < 0.0000005:
            return None
        return (
            f"{_tokens(received_text, currency)} arrived, {'less' if got < want else 'more'} than the "
            f"{_tokens(asked, currency)} the invoice asked for, so we added what arrived, at its value when it "
            "arrived."
        )
    return None


def describe_invoice(inv: dict, received: dict | None = None) -> tuple[str | None, str | None]:
    """(status_message, next_step) for one of a customer's invoices.

    `inv` has the invoice's fields plus review_status, decision_reason and
    decision_note; `received` is the credited deposit for it, if any
    ({"amount", "matched_by"})."""
    currency = inv["currency"]
    status = inv["status"]
    review = inv.get("review_status")
    # Paid wins over whatever a review said before: the watcher or a wallet
    # link can credit an invoice after its review was dismissed.
    if status == "confirmed":
        if not received:
            return (None, None)
        asked = inv.get("amount_to_send") or f"{inv['amount_crypto']:.6f}"
        note = credited_note(
            received.get("matched_by"), asked=asked, received=received["amount"], currency=currency,
            paid_currency=received.get("currency"),
        )
        return (note, None)
    if status == "rejected":
        why, step = explain_reason(inv.get("decision_reason"), inv.get("decision_note"), currency)
        return (f"This invoice was cancelled. {why}".strip(), step)
    if review == "open":
        return (
            "We're checking the payment you reported. We'll email you when it's done.",
            None,
        )
    if review == "dismissed":
        why, step = explain_reason(inv.get("decision_reason"), inv.get("decision_note"), currency)
        head = (
            "We checked the payment you reported."
            if inv.get("decision_reason") == "already_credited"
            else "We checked the payment you reported and didn't add it."
        )
        return (f"{head} {why}".strip(), step)
    if status == "expired":
        return (
            "This invoice expired without a payment we could match to it.",
            "Paid it? Ask us to check the payment with its transaction hash, or verify the wallet you paid from: "
            "payments from a verified wallet are added automatically.",
        )
    return (None, None)

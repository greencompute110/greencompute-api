"""Accepting gamma: crediting what hotkeys spend at our service, the account
a signed request acts as, and the public balance and totals.

Whose account a hotkey's gamma is credited to:
- a hotkey an account verified (Billing > Verified wallets, signed with the
  hotkey) pays into that account, like a TAO wallet;
- otherwise the hotkey becomes an account of its own, as a wallet sign-in
  would make it (no email; it signs in with the hotkey). This is engy's
  "the hotkey is the account" and what Lium opens on the first sign-in.
- spends from a deposit our treasury coldkey funded (by default SN110's
  owner coldkey), or by a treasury hotkey, are buybacks: recorded, never
  credited.
- a hotkey or deposit coldkey on a sanctions list is never credited.

Credit is $1 per gamma (plus BONUS_RATES["gamma"], 0 today). A total's
growth is credited, the cursor moved and the spend logged in one
transaction, so a total read twice, or growing again before a retry, can
never be credited twice; whole cents are credited and a fraction carries
over to the next spend.
"""
from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select

from greencompute_persistence import session_scope
from greencompute_persistence.account_events import add_event, payment_event
from greencompute_persistence.credentials import stored_api_key
from greencompute_persistence.orm import (
    APIKeyORM,
    CryptoDepositORM,
    GammaSpendORM,
    GammaSpentTotalORM,
    KeyValueStateORM,
    LedgerEntryORM,
    UserORM,
    VerifiedWalletORM,
)
from greencompute_gateway.domain import gamma as g
from greencompute_gateway.infrastructure.wallet_signatures import InvalidAddress, normalize_address, wallet_tag

log = logging.getLogger(__name__)

CHAIN = "bittensor"
CURRENCY = "gamma"
#: key_value_state: the last finalized block the watcher read every total at
#: (the billing monitor alerts when it stops moving) and what it last saw of
#: gamma on the subnet.
STATE_KEY = "watcher:gamma:last_block"
CHAIN_STATUS_KEY = "gamma:chain_status"
# Alerts sent in this process, so a retried block doesn't repeat one.
_ALERTED: set[str] = set()


class RetryLater(Exception):
    """This growth can't be decided now (sanctions lists not loaded yet for a
    new account): leave its total as it was, the next read retries it."""


def _now() -> datetime:
    return datetime.now(UTC)


def _short(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}" if len(address) > 12 else address


def spend_id(netuid: int, counter: int, service: str, hotkey: str, total_raw: int) -> str:
    return f"gamma:{netuid}:{counter}:{service}:{hotkey}:{total_raw}"


def _wallet_owner(session_factory, address: str) -> str | None:
    with session_scope(session_factory) as session:
        return session.scalar(select(VerifiedWalletORM.user_id).where(
            VerifiedWalletORM.chain == CHAIN, VerifiedWalletORM.address == address))


def _alert_once(key: str, text: str) -> None:
    if key in _ALERTED:
        return
    _ALERTED.add(key)
    from greencompute_gateway.infrastructure.alerts import send_payment_alert

    send_payment_alert(text)


# --- crediting ----------------------------------------------------------------------


def credit_growth(
    session_factory,
    *,
    netuid: int,
    counter: int,
    service: str,
    hotkey: str,
    total_raw: int,
    block: int,
    coldkey: str | None = None,
    billing=None,
) -> str:
    """Handle the chain's GammaSpent(netuid, service, hotkey) = `total_raw`
    read at finalized `block`. Returns credited | buyback | blocked |
    logged (grew by under a cent's worth, carried over) | seen (not grown).
    Raises RetryLater when it can't be decided yet."""
    from greencompute_gateway.application import sanctions, wallet_auth

    address = normalize_address(CHAIN, hotkey)
    funder = _canonical(coldkey) if coldkey else None
    treasury = (address in {_canonical(h) for h in g.treasury_hotkeys()}
                or (funder is not None and funder in {_canonical(c) for c in g.treasury_coldkeys()}))
    owner: str | None = None
    source: str | None = None
    if treasury:
        kind = "treasury"
    else:
        owner = _wallet_owner(session_factory, address)
        try:
            source = sanctions.screen(session_factory, CHAIN, address, new_account=owner is None)
            if not source and coldkey:
                source = sanctions.screen(session_factory, CHAIN, funder or coldkey)
        except sanctions.ScreeningUnavailable as exc:
            raise RetryLater("sanctions lists not loaded yet") from exc
        if source:
            kind = "blocked"
        else:
            kind = "customer"
            if owner is None:
                owner = wallet_auth.create_wallet_account(session_factory, CHAIN, address) or _wallet_owner(
                    session_factory, address)
                if owner is None:
                    raise RetryLater("the hotkey's account is being created")
                log.info("gamma: new account %s for hotkey %s", owner, _short(address))

    outcome, cents, balance_after, amount = _apply(
        session_factory, netuid=netuid, counter=counter, service=service, address=address, total_raw=total_raw,
        block=block, kind=kind, owner=owner, source=source,
    )
    if outcome == "seen":
        return outcome
    log.info("gamma: %s gamma from %s at block %d -> %s (%d cents)", g.gamma_text(amount), _short(address), block,
             outcome, cents)
    if outcome == "blocked":
        if owner:
            sanctions.flag_wallet(session_factory, CHAIN, address, source)
        _alert_once(f"blocked:{address}:{total_raw}", (
            f"Blocked gamma payment: {g.gamma_text(amount)} gamma spent at our service by hotkey {address}"
            + (f" (deposit coldkey {coldkey})" if coldkey else "")
            + f" at block {block} was NOT credited: sanctions screening ({source}) names the sender."
            + (f" The hotkey is verified by account {owner}, which can no longer rent." if owner else "")
        ))
    elif outcome == "credited" and billing is not None:
        dep = {"deposit_ref": spend_id(netuid, counter, service, address, total_raw), "currency": CURRENCY,
               "amount_raw": amount, "decimals": 9, "tx_hash": None}
        billing._email_deposit(owner, dep, cents, balance_after, bonus_pct=g.bonus_pct())
    return outcome


def _canonical(hotkey: str) -> str | None:
    try:
        return normalize_address(CHAIN, hotkey)
    except InvalidAddress:
        return None


def _apply(session_factory, *, netuid, counter, service, address, total_raw, block, kind, owner, source):
    """The transaction: re-read the total under its row lock, credit or log
    the growth since, move the total. (outcome, cents, balance_after, gamma)."""
    now = _now()
    with session_scope(session_factory) as session:
        row = session.get(GammaSpentTotalORM, (netuid, service, address), with_for_update=True)
        if row is not None and row.subnet_counter > counter:
            return "seen", 0, None, 0  # read before the subnet was registered again
        same = row is not None and row.subnet_counter == counter
        prev_total = row.total_raw if same else 0
        prev_cents = row.credited_cents if same else 0
        if total_raw <= prev_total:
            return "seen", 0, None, 0
        amount = total_raw - prev_total
        ref = spend_id(netuid, counter, service, address, total_raw)
        if session.get(GammaSpendORM, ref) is not None:
            return "seen", 0, None, 0

        cents, balance_after = 0, None
        outcome = {"treasury": "buyback", "blocked": "blocked"}.get(kind, "credited")
        deposit = dict(
            deposit_ref=ref, currency=CURRENCY, tx_hash=None, sender=address, deposit_address=service,
            amount_raw=amount, decimals=9, block_number=block, usd_price=1.0, observed_at=now,
        )
        if kind == "customer":
            base = g.base_cents(total_raw) - g.base_cents(prev_total)
            cents = base + int(base * g.bonus_pct())
            if cents > 0:
                user = session.get(UserORM, owner, with_for_update=True)
                if user is None:
                    raise KeyError(f"user {owner} not found")
                user.balance_credits += cents
                balance_after = user.balance_credits
                session.add(LedgerEntryORM(
                    entry_id=str(uuid4()), user_id=owner, amount_cents=cents, balance_after=balance_after,
                    kind="topup", deposit_ref=ref,
                    description=f"Crypto deposit GAMMA {g.gamma_text(amount)} spent by hotkey {_short(address)}",
                    created_at=now,
                ))
                session.add(CryptoDepositORM(
                    **deposit, status="credited", matched_by="gamma", user_id=owner, credited_cents=cents,
                    credited_at=now,
                ))
                add_event(session, payment_event(
                    owner, cents=cents, balance_after=balance_after, method="crypto", currency=CURRENCY,
                    sender=address, matched_by="gamma",
                ))
            else:
                outcome = "logged"
        elif kind == "blocked":
            session.add(CryptoDepositORM(
                **deposit, status="blocked", matched_by=f"sanctions:{source}"[:32], user_id=owner,
            ))

        session.add(GammaSpendORM(
            spend_id=ref, netuid=netuid, subnet_counter=counter, service_hotkey=service, hotkey=address,
            kind=kind, amount_raw=amount, total_raw=total_raw, block_number=block,
            user_id=owner if kind == "customer" else None, credited_cents=cents if kind == "customer" else None,
            observed_at=now,
        ))
        if row is None:
            row = GammaSpentTotalORM(netuid=netuid, service_hotkey=service, hotkey=address)
            session.add(row)
        row.subnet_counter = counter
        row.total_raw = total_raw
        row.credited_cents = prev_cents + cents
        row.kind = kind
        row.user_id = owner if kind == "customer" else row.user_id
        row.last_block = block
        row.updated_at = now
        return outcome, cents, balance_after, amount


# --- signed requests ----------------------------------------------------------------


def signed_request_key(session_factory, hotkey: str):
    """The API key a btauth/1 request signed by `hotkey` acts as: one per
    account and hotkey, scopes SIGNED_REQUEST_SCOPES, made on first use. Its
    secret is never shown; it goes the way of the hotkey's other keys (a new
    sign-in, turning the hotkey's sign-in off, unlinking it) and comes back
    on the next signed request while the hotkey may still sign in."""
    from greencompute_gateway.transport.errors import coded

    address = _canonical(hotkey)
    if address is None:
        raise coded(401, "signed_request_invalid", {"message": "X-Bittensor-Hotkey isn't a valid hotkey",
                                                     "reason": "malformed"})
    tag = wallet_tag(CHAIN, address)
    with session_scope(session_factory) as session:
        wallet = session.scalar(select(VerifiedWalletORM).where(
            VerifiedWalletORM.chain == CHAIN, VerifiedWalletORM.address == address))
        if wallet is None:
            raise coded(402, "gamma_payment_required", {
                "message": (
                    "This hotkey has no Green Compute account yet. Spend SN110 gamma at our service hotkey "
                    f"{g.service_hotkey()} and the hotkey's account is opened and credited $1 per gamma within "
                    "about a minute; or sign in with the hotkey (POST /platform/auth/wallet/challenge)."
                ),
                "hotkey": hotkey,
                "pay": g.how_to_pay(hotkey),
            })
        if not wallet.sign_in:
            raise coded(403, "wallet_sign_in_off", (
                "This hotkey is linked to an account to pay in, not to sign requests. Sign in to that account and "
                "turn on \"Sign in with this wallet\" for it under Billing > Verified wallets."
            ))
        user_id = wallet.user_id
        key = session.scalar(select(APIKeyORM).where(
            APIKeyORM.user_id == user_id, APIKeyORM.issued_by_wallet == tag,
            APIKeyORM.name == g.SIGNED_REQUEST_KEY_NAME,
        ).limit(1))
        if key is not None:
            return stored_api_key(key)
        # The account's lock, as every key a wallet's sign-in makes takes it,
        # so two first requests can't make two keys or race a revocation.
        user = session.get(UserORM, user_id, with_for_update=True)
        if user is None or (user.username or "").startswith("deleted-"):
            raise coded(403, "forbidden", "The account this hotkey signs in to no longer exists. "
                                          "Contact support@green-compute.com.")
        key = session.scalar(select(APIKeyORM).where(
            APIKeyORM.user_id == user_id, APIKeyORM.issued_by_wallet == tag,
            APIKeyORM.name == g.SIGNED_REQUEST_KEY_NAME,
        ).limit(1))
        if key is None:
            key = APIKeyORM(
                key_id=str(uuid4()), user_id=user_id, name=g.SIGNED_REQUEST_KEY_NAME, admin=False,
                scopes=list(g.SIGNED_REQUEST_SCOPES), secret=f"gk_{secrets.token_urlsafe(32)}", created_at=_now(),
                issued_by_wallet=tag,
            )
            session.add(key)
            session.flush()
            log.info("gamma: signed-request key %s for account %s (hotkey %s)", key.key_id, user_id, _short(address))
        return stored_api_key(key)


# --- what anyone may read -----------------------------------------------------------


def last_block(session_factory) -> int | None:
    with session_scope(session_factory) as session:
        row = session.get(KeyValueStateORM, STATE_KEY)
        try:
            return int(row.value) if row and row.value else None
        except ValueError:
            return None


def _only_this_hotkeys_gamma(session, user: UserORM, wallet: VerifiedWalletORM, credited: int) -> bool:
    """True for an account that is nothing but this hotkey's gamma: opened
    for the wallet, no email, no other wallet, and every cent ever added to
    it is gamma this hotkey spent. Its balance says nothing the chain's
    public record of those spends doesn't already start to say."""
    if credited <= 0 or not wallet.sign_in or user.signup_method != "wallet" or (user.email or "").strip():
        return False
    wallets = session.scalar(select(func.count()).select_from(VerifiedWalletORM).where(
        VerifiedWalletORM.user_id == user.user_id)) or 0
    if wallets != 1:
        return False
    added = int(session.scalar(select(func.coalesce(func.sum(LedgerEntryORM.amount_cents), 0)).where(
        LedgerEntryORM.user_id == user.user_id, LedgerEntryORM.amount_cents > 0)) or 0)
    return added == credited


def balance(session_factory, hotkey: str, *, signed_by: str | None = None) -> dict:
    """The standard's balance for a hotkey, in USD as decimal strings.

    `credited_usd`, what the hotkey's gamma spends here were credited, is
    public: the chain shows the spends to anyone. What is used and what is
    left are the account's business, so `used_usd` and `remaining_usd` are
    given only
    - to a request signed by the hotkey itself (btauth/1; `signed_by`), when
      the hotkey signs in to its account, or
    - for an account that is nothing but this hotkey's gamma (the hotkey is
      the account, as at Lium and engy);
    and are null otherwise: never the balance of an account that was funded
    another way, or that only linked the hotkey to pay in. Zeros for a
    hotkey that never spent here and has nothing to show."""
    address = _canonical(hotkey)
    if address is None:
        raise ValueError("not a hotkey")
    own = signed_by is not None and _canonical(signed_by) == address
    used: int | None = None
    remaining: int | None = None
    with session_scope(session_factory) as session:
        credited = int(session.scalar(select(func.coalesce(func.sum(GammaSpendORM.credited_cents), 0)).where(
            GammaSpendORM.hotkey == address, GammaSpendORM.kind == "customer",
        )) or 0)
        wallet = session.scalar(select(VerifiedWalletORM).where(
            VerifiedWalletORM.chain == CHAIN, VerifiedWalletORM.address == address))
        user = session.get(UserORM, wallet.user_id) if wallet is not None else None
        if user is not None and ((own and wallet.sign_in) or _only_this_hotkeys_gamma(session, user, wallet, credited)):
            remaining = max(0, int(user.balance_credits or 0))
            used = max(0, credited - remaining)
        elif credited == 0 and (not own or wallet is None):
            used, remaining = 0, 0  # nothing paid here: nothing to show, account or not
    out = {
        "hotkey": hotkey,
        "credited_usd": g.usd_text(credited),
        "used_usd": g.usd_text(used) if used is not None else None,
        "remaining_usd": g.usd_text(remaining) if remaining is not None else None,
        "as_of_block": last_block(session_factory),
    }
    if remaining is None:
        out["note"] = ("used_usd and remaining_usd are shown to the hotkey itself: sign this request with it "
                       "(btauth/1), on an account the hotkey signs in to.")
    return out


def spent_totals(session_factory, *, recent: int = 20) -> dict:
    """What has been spent at our service, as the watcher last read it:
    customers' payments, our buybacks, and spends that were blocked; plus
    the latest buybacks. Anyone can check the sum on chain
    (GammaSpent(netuid, service, *))."""
    netuid = g.netuid()
    with session_scope(session_factory) as session:
        rows = session.execute(
            select(GammaSpentTotalORM.kind, func.coalesce(func.sum(GammaSpentTotalORM.total_raw), 0),
                   func.count())
            .where(GammaSpentTotalORM.netuid == netuid)
            .group_by(GammaSpentTotalORM.kind)
        ).all()
        buybacks = session.scalars(
            select(GammaSpendORM).where(GammaSpendORM.netuid == netuid, GammaSpendORM.kind == "treasury")
            .order_by(GammaSpendORM.block_number.desc()).limit(recent)
        ).all()
        recent_buybacks = [{
            "gamma": g.gamma_text(b.amount_raw), "read_at_block": b.block_number, "hotkey": b.hotkey,
            "observed_at": b.observed_at.isoformat() if b.observed_at else None,
        } for b in buybacks]
    by_kind = {kind: (int(total), int(n)) for kind, total, n in rows}
    customer, payers = by_kind.get("customer", (0, 0))
    treasury = by_kind.get("treasury", (0, 0))[0]
    blocked = by_kind.get("blocked", (0, 0))[0]
    return {
        "netuid": netuid,
        "service_hotkeys": g.service_hotkeys(),
        "treasury_coldkeys": sorted(g.treasury_coldkeys()),
        "treasury_hotkeys": sorted(g.treasury_hotkeys()),
        "paid_by_customers_gamma": g.gamma_text(customer),
        "paying_hotkeys": payers,
        "bought_back_gamma": g.gamma_text(treasury),
        "blocked_gamma": g.gamma_text(blocked),
        "total_spent_gamma": g.gamma_text(customer + treasury + blocked),
        "as_of_block": last_block(session_factory),
        "recent_buybacks": recent_buybacks,
        "check_on_chain": "SubtensorModule.GammaSpent(netuid, service hotkey, *) at any finalized block",
    }


def chain_status(session_factory) -> dict | None:
    import json

    with session_scope(session_factory) as session:
        row = session.get(KeyValueStateORM, CHAIN_STATUS_KEY)
        if row is None or not row.value:
            return None
        try:
            return json.loads(row.value)
        except ValueError:
            return None

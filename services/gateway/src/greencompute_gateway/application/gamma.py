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

The weekly limit. Gamma pays us nothing in hand (the customer burned alpha
to mint it), so the owner limits how much credit it buys: at most
`weekly_limit` cents of gamma credit a UTC week (Monday to Sunday; $1,000
until staff set another, None for no limit). A spend is never refused or
lost, the chain has already destroyed the gamma: what fits under the limit
is credited at once and the rest WAITS on its gamma_spends row
(waiting_cents) and is credited in a later week, oldest spend first
(release_waiting, run by the watcher every tick). A new spend never jumps
ahead of gamma that is waiting. What was credited in a week is summed from
the ledger, the record of credit itself. Our own buybacks are not credit
and don't count. Every transaction that credits gamma first takes the lock
on the settings row (_take_turn), so the week's sum stays exact even if two
processes ever ran the watcher. Gamma that waited is screened again before
it is credited: a hotkey, deposit coldkey or account listed meanwhile is
never credited.
"""
from __future__ import annotations

import json
import logging
import secrets
from datetime import UTC, date, datetime, timedelta
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
from greencompute_gateway.infrastructure.wallet_signatures import (
    SIGNED_TAG_SUFFIX,
    InvalidAddress,
    normalize_address,
    wallet_tag,
)

log = logging.getLogger(__name__)

CHAIN = "bittensor"
CURRENCY = "gamma"
#: key_value_state: the last finalized block the watcher read every total at
#: (the billing monitor alerts when it stops moving) and what it last saw of
#: gamma on the subnet.
STATE_KEY = "watcher:gamma:last_block"
CHAIN_STATUS_KEY = "gamma:chain_status"
#: key_value_state: {"weekly_limit_cents": 100000 | null}, set by staff.
SETTINGS_KEY = "gamma:settings"
#: Gamma credit a UTC week until staff set another: $1,000.
DEFAULT_WEEKLY_LIMIT_CENTS = 100_000
MAX_WEEKLY_LIMIT_USD = 100_000_000
# Waiting spends credited per tick at most (the rest next tick, 12 s on).
RELEASE_BATCH = 200
# Alerts sent in this process, so a retried block doesn't repeat one.
_ALERTED: set[str] = set()


class RetryLater(Exception):
    """This growth can't be decided now (sanctions lists not loaded yet for a
    new account): leave its total as it was, the next read retries it."""


class GammaSettingsError(ValueError):
    """A setting that can't be made as asked; the message says why."""


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


# --- the weekly limit ---------------------------------------------------------------


def _limit_cents(session) -> int | None:
    """Cents of gamma credit a week; None: no limit."""
    row = session.get(KeyValueStateORM, SETTINGS_KEY)
    if row is None or not row.value:
        return DEFAULT_WEEKLY_LIMIT_CENTS
    try:
        stored = json.loads(row.value)
    except ValueError:
        return DEFAULT_WEEKLY_LIMIT_CENTS
    if not isinstance(stored, dict) or "weekly_limit_cents" not in stored:
        return DEFAULT_WEEKLY_LIMIT_CENTS
    value = stored["weekly_limit_cents"]
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return DEFAULT_WEEKLY_LIMIT_CENTS


def get_settings(session_factory) -> dict:
    with session_scope(session_factory) as session:
        limit = _limit_cents(session)
    return {"weekly_limit_usd": None if limit is None else limit / 100}


def set_weekly_limit(session_factory, usd) -> dict:
    """Set the weekly limit in USD; None removes it; 0 makes every spend
    wait. Gamma already waiting is credited by the next watcher tick as far
    as the new limit allows."""
    if usd is not None:
        if isinstance(usd, bool) or not isinstance(usd, (int, float)) or not 0 <= float(usd) <= MAX_WEEKLY_LIMIT_USD:
            raise GammaSettingsError(
                f"weekly_limit_usd must be a number from 0 to {MAX_WEEKLY_LIMIT_USD:,}, or null for no limit")
    cents = None if usd is None else round(float(usd) * 100)
    with session_scope(session_factory) as session:
        row = session.get(KeyValueStateORM, SETTINGS_KEY) or KeyValueStateORM(key=SETTINGS_KEY)
        row.value = json.dumps({"weekly_limit_cents": cents})
        row.updated_at = _now()
        session.add(row)
    _status_cache["at"] = 0.0
    return get_settings(session_factory)


def week_start(now: datetime) -> datetime:
    """Monday 00:00 UTC of `now`'s week."""
    day = now.astimezone(UTC).date()
    monday = day - timedelta(days=day.weekday())
    return datetime(monday.year, monday.month, monday.day, tzinfo=UTC)


def _credited_since(session, start: datetime) -> int:
    """Cents of gamma credit given since `start`, from the ledger: every
    gamma credit is a top-up whose deposit_ref starts "gamma:"."""
    return int(session.scalar(
        select(func.coalesce(func.sum(LedgerEntryORM.amount_cents), 0)).where(
            LedgerEntryORM.kind == "topup", LedgerEntryORM.amount_cents > 0,
            LedgerEntryORM.deposit_ref.like("gamma:%"), LedgerEntryORM.created_at >= start,
        )) or 0)


def _waiting_total(session) -> int:
    return int(session.scalar(
        select(func.coalesce(func.sum(GammaSpendORM.waiting_cents), 0)).where(
            GammaSpendORM.kind == "customer", GammaSpendORM.waiting_cents > 0)) or 0)


def _room(session, now: datetime) -> int | None:
    """Cents that can still be credited this week; None: no limit."""
    limit = _limit_cents(session)
    if limit is None:
        return None
    return max(0, limit - _credited_since(session, week_start(now)))


def _take_turn(session) -> None:
    """Hold the settings row until the transaction ends. Called first by
    every transaction that credits gamma, so two of them can never read the
    same room under the weekly limit and both use it. The row is made on
    first use ({}: the default limit applies)."""
    row = session.get(KeyValueStateORM, SETTINGS_KEY, with_for_update=True)
    if row is None:
        session.add(KeyValueStateORM(key=SETTINGS_KEY, value="{}", updated_at=_now()))
        session.flush()


def _account_gone(session, user_id: str | None) -> bool:
    """True when the account no longer exists or its owner deleted it. Reads
    columns only: the account's row must first be loaded under its lock
    (_credit), or a balance changed meanwhile would be overwritten."""
    if not user_id:
        return True
    found = session.execute(select(UserORM.profile_metadata).where(UserORM.user_id == user_id)).first()
    return found is None or bool((found[0] or {}).get("deleted_at"))


def _credit(session, *, owner: str, cents: int, ledger_ref: str, spend_ref: str, address: str, service: str,
            amount_raw: int, block: int, tx_hash: str | None, observed_at: datetime, now: datetime,
            part: bool = False) -> int:
    """Add `cents` of a gamma spend to its account: balance, ledger top-up,
    the deposit log's row for the spend (one per spend, however many parts
    it is credited in) and the payment event. Returns the balance after."""
    user = session.get(UserORM, owner, with_for_update=True)
    if user is None:
        raise KeyError(f"user {owner} not found")
    user.balance_credits += cents
    balance_after = user.balance_credits
    session.add(LedgerEntryORM(
        entry_id=str(uuid4()), user_id=owner, amount_cents=cents, balance_after=balance_after,
        kind="topup", deposit_ref=ledger_ref,
        description=(f"Crypto deposit GAMMA {g.gamma_text(amount_raw)} spent by hotkey {_short(address)}"
                     + (" (credited in parts: weekly gamma limit)" if part else "")),
        created_at=now,
    ))
    deposit = session.get(CryptoDepositORM, spend_ref)
    if deposit is None:
        session.add(CryptoDepositORM(
            deposit_ref=spend_ref, currency=CURRENCY, tx_hash=tx_hash, sender=address, deposit_address=service,
            amount_raw=amount_raw, decimals=9, block_number=block, usd_price=1.0, observed_at=observed_at,
            status="credited", matched_by="gamma", user_id=owner, credited_cents=cents, credited_at=now,
        ))
    else:
        deposit.credited_cents = int(deposit.credited_cents or 0) + cents
        deposit.credited_at = now
    add_event(session, payment_event(
        owner, cents=cents, balance_after=balance_after, method="crypto", currency=CURRENCY,
        tx_hash=tx_hash, sender=address, matched_by="gamma",
    ))
    return balance_after


def _email_part(billing, owner: str, *, ledger_ref: str, cents: int, balance_after: int | None,
                tx_hash: str | None, waiting: int) -> None:
    """The payment email for gamma credited: the whole spend, or the part
    of it the weekly limit let through."""
    if billing is None:
        return
    bonus = g.bonus_pct()
    # The gamma this credit stands for ($1 each, before any bonus).
    raw = round(cents / (1 + bonus)) * g.RAW_PER_GAMMA // 100
    note = None
    if waiting > 0:
        note = (f"Another ${g.usd_text(waiting)} of this gamma is waiting for our weekly gamma limit. It is "
                "credited in a later week, oldest first; nothing is lost.")
    try:
        billing._email_deposit(
            owner, {"deposit_ref": ledger_ref, "currency": CURRENCY, "amount_raw": raw, "decimals": 9,
                    "tx_hash": tx_hash},
            cents, balance_after, bonus_pct=bonus, note=note)
    except Exception:  # noqa: BLE001 - an email must never undo or repeat a credit
        log.exception("gamma: payment email for %s failed", ledger_ref)


def release_waiting(session_factory, billing=None, *, now: datetime | None = None) -> int:
    """Credit gamma that is waiting for the weekly limit, oldest spend first,
    as far as this week's room goes (all of it when there is no limit).
    Returns how many spends got credit.

    Before a spend is credited it is screened again (its hotkey, its deposit
    coldkey and its account's wallets): what a sanctions list names since is
    not credited, and staff are told. Waiting gamma of an account deleted
    since (delete_account normally ends the wait itself) is dropped, as the
    account's balance was. Neither holds up the spends behind it."""
    from greencompute_gateway.application import sanctions

    now = now or _now()
    done: list[tuple] = []
    dropped: list[tuple] = []
    blocked: list[tuple] = []
    with session_scope(session_factory) as session:
        _take_turn(session)
        rows = session.scalars(
            select(GammaSpendORM).where(GammaSpendORM.kind == "customer", GammaSpendORM.waiting_cents > 0)
            .order_by(GammaSpendORM.observed_at, GammaSpendORM.block_number, GammaSpendORM.spend_id)
            .limit(RELEASE_BATCH).with_for_update()
        ).all()
        if not rows:
            return 0
        room = _room(session, now)
        for row in rows:
            if _account_gone(session, row.user_id):
                dropped.append((row.spend_id, row.hotkey, row.waiting_cents, row.user_id))
                row.waiting_cents = 0
                continue
            if room is not None and room <= 0:
                continue  # keep looking for deleted accounts; nothing more fits this week
            source = (sanctions.screen(session_factory, CHAIN, row.hotkey)
                      or (row.coldkey and sanctions.screen(session_factory, CHAIN, row.coldkey))
                      or sanctions.account_blocked(session_factory, row.user_id))
            if source:
                blocked.append((row.spend_id, row.hotkey, row.coldkey, row.waiting_cents, row.user_id, source))
                if session.get(CryptoDepositORM, row.spend_id) is None:
                    # Nothing of it was credited: log it as a blocked
                    # deposit, as a spend blocked on arrival is.
                    session.add(CryptoDepositORM(
                        deposit_ref=row.spend_id, currency=CURRENCY, tx_hash=row.tx_hash, sender=row.hotkey,
                        deposit_address=row.service_hotkey, amount_raw=row.amount_raw, decimals=9,
                        block_number=row.block_number, usd_price=1.0, observed_at=row.observed_at,
                        status="blocked", matched_by=f"sanctions:{source}"[:32], user_id=row.user_id,
                    ))
                row.waiting_cents = 0
                continue
            pay = row.waiting_cents if room is None else min(row.waiting_cents, room)
            credited_before = int(row.credited_cents or 0)
            # The spend's first credit carries its own id, like a spend
            # credited at once; later parts add what was credited before.
            ledger_ref = row.spend_id if credited_before == 0 else f"{row.spend_id}#{credited_before}"
            balance_after = _credit(
                session, owner=row.user_id, cents=pay, ledger_ref=ledger_ref, spend_ref=row.spend_id,
                address=row.hotkey, service=row.service_hotkey, amount_raw=row.amount_raw, block=row.block_number,
                tx_hash=row.tx_hash, observed_at=row.observed_at, now=now, part=True,
            )
            row.credited_cents = credited_before + pay
            row.waiting_cents -= pay
            total = session.get(GammaSpentTotalORM, (row.netuid, row.service_hotkey, row.hotkey), with_for_update=True)
            if total is not None and total.subnet_counter == row.subnet_counter:
                total.credited_cents = int(total.credited_cents or 0) + pay
            if room is not None:
                room -= pay
            done.append((row.user_id, ledger_ref, pay, balance_after, row.tx_hash, row.waiting_cents, row.hotkey))
    _status_cache["at"] = 0.0
    for spend, hotkey, cents, user_id in dropped:
        log.warning("gamma: %d cents waiting for deleted account %s (hotkey %s) dropped", cents, user_id,
                    _short(hotkey))
        _alert_once(f"dropped:{spend}", (
            f"Gamma: ${g.usd_text(cents)} of gamma spent by hotkey {hotkey} was waiting for the weekly limit, and "
            f"its account ({user_id}) has been deleted since. It was not credited, like the balance the account "
            f"gave up. Spend {spend}."
        ))
    for spend, hotkey, coldkey, cents, user_id, source in blocked:
        log.warning("gamma: %d cents waiting for %s (hotkey %s) blocked: %s", cents, user_id, _short(hotkey), source)
        sanctions.flag_wallet(session_factory, CHAIN, hotkey, source)
        _alert_once(f"blocked-waiting:{spend}", (
            f"Blocked gamma payment: ${g.usd_text(cents)} of gamma spent by hotkey {hotkey}"
            + (f" (deposit coldkey {coldkey})" if coldkey else "")
            + f" was waiting for the weekly limit and was NOT credited: sanctions screening ({source}) now names "
              f"the sender or the account ({user_id}). Spend {spend}."
        ))
    for owner, ledger_ref, cents, balance_after, tx_hash, waiting, hotkey in done:
        log.info("gamma: %d cents that waited for the weekly limit credited to %s (hotkey %s); %d still waiting",
                 cents, owner, _short(hotkey), waiting)
        _email_part(billing, owner, ledger_ref=ledger_ref, cents=cents, balance_after=balance_after,
                    tx_hash=tx_hash, waiting=waiting)
    return len(done)


_status_cache: dict = {"at": 0.0, "value": None}
STATUS_CACHE_SECONDS = 10


def limit_status(session_factory, *, now: datetime | None = None, fresh: bool = False) -> dict:
    """The weekly limit as anyone may read it before spending: what it is,
    what this week has used, what is waiting, and from when gamma spent now
    would be credited. At most 10 s old unless `fresh` (or `now` is given)."""
    import time

    if not fresh and now is None and _status_cache["value"] is not None \
            and _status_cache.get("for") is session_factory \
            and time.monotonic() - _status_cache["at"] < STATUS_CACHE_SECONDS:
        return _status_cache["value"]
    at = now or _now()
    start = week_start(at)
    with session_scope(session_factory) as session:
        limit = _limit_cents(session)
        credited = _credited_since(session, start)
        waiting = _waiting_total(session)
    monday = start.date()
    remaining = None if limit is None else max(0, limit - credited)
    next_from: date | None = at.astimezone(UTC).date()
    if limit is None:
        credits_now = True
    else:
        # Gamma that is waiting takes this week's room first.
        free = remaining - waiting
        credits_now = free > 0
        if not credits_now:
            next_from = None if limit == 0 else monday + timedelta(days=7 * ((-free) // limit + 1))
    if limit is None:
        note = "There is no weekly limit: gamma is credited within about a minute of the spend finalizing."
    elif limit == 0:
        note = ("Gamma credit is paused. Gamma spent now is not lost: it waits and is credited, oldest spend "
                "first, when we open it again.")
    elif credits_now:
        note = (f"We credit up to ${g.usd_text(limit)} of gamma a week (Monday to Sunday, UTC). Gamma spent above "
                "what is left this week is not lost: it waits and is credited in a later week, oldest spend first.")
    else:
        note = (f"This week's ${g.usd_text(limit)} of gamma credit is used. Gamma spent now is not lost: it "
                f"waits and is credited from {next_from.isoformat()}, oldest spend first.")
    out = {
        "weekly_usd": None if limit is None else g.usd_text(limit),
        "week_start": monday.isoformat(),
        "week_end": (monday + timedelta(days=6)).isoformat(),
        "credited_this_week_usd": g.usd_text(credited),
        "remaining_this_week_usd": None if remaining is None else g.usd_text(remaining),
        "waiting_usd": g.usd_text(waiting),
        "credits_now": credits_now,
        "next_credit_from": next_from.isoformat() if next_from else None,
        "note": note,
    }
    if now is None:
        _status_cache.update({"at": time.monotonic(), "value": out, "for": session_factory})
    return out


def account_gamma(session_factory, user_id: str, *, recent: int = 50) -> dict:
    """An account's own gamma: what its hotkeys spent here, what of each
    spend is credited and what still waits for the weekly limit, newest
    first, with where the week stands."""
    with session_scope(session_factory) as session:
        rows = session.scalars(
            select(GammaSpendORM).where(GammaSpendORM.user_id == user_id, GammaSpendORM.kind == "customer")
            .order_by(GammaSpendORM.observed_at.desc(), GammaSpendORM.block_number.desc()).limit(recent)
        ).all()
        credited, waiting = session.execute(
            select(func.coalesce(func.sum(GammaSpendORM.credited_cents), 0),
                   func.coalesce(func.sum(GammaSpendORM.waiting_cents), 0)).where(
                GammaSpendORM.user_id == user_id, GammaSpendORM.kind == "customer")).one()
        spends = [{
            "hotkey": r.hotkey, "gamma": g.gamma_text(r.amount_raw), "tx_hash": r.tx_hash,
            "block": r.block_number, "credited_usd": g.usd_text(int(r.credited_cents or 0)),
            "waiting_usd": g.usd_text(int(r.waiting_cents or 0)),
            "spent_at": r.observed_at.isoformat() if r.observed_at else None,
        } for r in rows]
    limit = limit_status(session_factory)
    waiting = int(waiting or 0)
    return {
        "credited_usd": g.usd_text(int(credited or 0)),
        "waiting_usd": g.usd_text(waiting),
        # When what is waiting starts to be credited: the next week at the
        # latest, later if other customers' gamma is ahead of it.
        "waiting_note": (
            "This gamma was spent above our weekly gamma limit. It is not lost: it is credited in a later "
            "week, oldest spend first, and you are emailed when it is." if waiting else None),
        "spends": spends,
        "limit": limit,
    }


def waiting_spends(session_factory, limit: int = 200) -> list[dict]:
    """The gamma that is waiting, oldest first: for staff."""
    with session_scope(session_factory) as session:
        rows = session.scalars(
            select(GammaSpendORM).where(GammaSpendORM.kind == "customer", GammaSpendORM.waiting_cents > 0)
            .order_by(GammaSpendORM.observed_at, GammaSpendORM.block_number, GammaSpendORM.spend_id).limit(limit)
        ).all()
        return [{
            "spend_id": r.spend_id, "hotkey": r.hotkey, "user_id": r.user_id, "gamma": g.gamma_text(r.amount_raw),
            "credited_usd": g.usd_text(int(r.credited_cents or 0)), "waiting_usd": g.usd_text(r.waiting_cents),
            "observed_at": r.observed_at.isoformat() if r.observed_at else None,
        } for r in rows]


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
    tx_hash: str | None = None,
) -> str:
    """Handle the chain's GammaSpent(netuid, service, hotkey) = `total_raw`
    read at finalized `block` (`tx_hash`: the spend's extrinsic, when the
    watcher found its event). Returns credited (all of it, or the part the
    weekly limit let through) | waiting (all of it waits for the weekly
    limit) | buyback | blocked | logged (grew by under a cent's worth,
    carried over) | seen (not grown). Raises RetryLater when it can't be
    decided yet."""
    from greencompute_gateway.application import sanctions, wallet_auth

    address = normalize_address(CHAIN, hotkey)
    funder = _canonical(coldkey) if coldkey else None
    # Ours: a treasury hotkey, a service hotkey spending at itself, or any
    # hotkey whose deposit a treasury coldkey funded.
    own_hotkeys = {_canonical(h) for h in (*g.treasury_hotkeys(), *g.service_hotkeys())}
    treasury = (address in own_hotkeys
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

    outcome, cents, balance_after, amount, waiting = _apply(
        session_factory, netuid=netuid, counter=counter, service=service, address=address, total_raw=total_raw,
        block=block, kind=kind, owner=owner, source=source, tx_hash=tx_hash, coldkey=funder,
    )
    if outcome == "seen":
        return outcome
    log.info("gamma: %s gamma from %s at block %d -> %s (%d cents%s)", g.gamma_text(amount), _short(address), block,
             outcome, cents, f", {waiting} waiting for the weekly limit" if waiting else "")
    if waiting:
        _status_cache["at"] = 0.0
        monday = week_start(_now()).date().isoformat()
        _alert_once(f"limit:{monday}", (
            f"Gamma: the weekly limit is reached for the week of {monday}. ${g.usd_text(waiting)} spent by hotkey "
            f"{address} is waiting and will be credited in a later week, oldest first. Change the limit under "
            "Admin > Network report > Gamma if it should be credited sooner."
        ))
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
        ref = spend_id(netuid, counter, service, address, total_raw)
        if waiting:
            _email_part(billing, owner, ledger_ref=ref, cents=cents, balance_after=balance_after, tx_hash=tx_hash,
                        waiting=waiting)
        else:
            dep = {"deposit_ref": ref, "currency": CURRENCY, "amount_raw": amount, "decimals": 9,
                   "tx_hash": tx_hash}
            billing._email_deposit(owner, dep, cents, balance_after, bonus_pct=g.bonus_pct())
    return outcome


def _canonical(hotkey: str) -> str | None:
    try:
        return normalize_address(CHAIN, hotkey)
    except InvalidAddress:
        return None


def _apply(session_factory, *, netuid, counter, service, address, total_raw, block, kind, owner, source,
           tx_hash=None, coldkey=None):
    """The transaction: re-read the total under its row lock, credit or log
    the growth since, move the total. (outcome, cents credited now,
    balance_after, gamma, cents left waiting for the weekly limit)."""
    now = _now()
    with session_scope(session_factory) as session:
        if kind == "customer":
            _take_turn(session)  # before any other lock: see _take_turn
        row = session.get(GammaSpentTotalORM, (netuid, service, address), with_for_update=True)
        if row is not None and row.subnet_counter > counter:
            return "seen", 0, None, 0, 0  # read before the subnet was registered again
        same = row is not None and row.subnet_counter == counter
        if row is not None and row.subnet_counter < counter and total_raw >= row.total_raw > 0:
            # The subnet's counter grew but this total did not start over. A
            # dissolved subnet's totals are cleared, so this is the counter
            # being set for a subnet that wasn't dissolved (a runtime
            # backfill): carry on from the old total, never credit it again.
            _alert_once(f"counter:{netuid}:{counter}", (
                f"Gamma watcher: subnet {netuid}'s RegisteredSubnetCounter went from {row.subnet_counter} to "
                f"{counter} but spent totals at our service did not reset. Treated as the same subnet: only "
                "growth past the old totals is credited. Check the subnet was not dissolved and re-registered."
            ))
            same = True
            if total_raw == row.total_raw:
                row.subnet_counter = counter
                return "seen", 0, None, 0, 0
        prev_total = row.total_raw if same else 0
        prev_cents = row.credited_cents if same else 0
        if total_raw <= prev_total:
            return "seen", 0, None, 0, 0
        amount = total_raw - prev_total
        ref = spend_id(netuid, counter, service, address, total_raw)
        if session.get(GammaSpendORM, ref) is not None:
            return "seen", 0, None, 0, 0

        cents, waiting, balance_after = 0, 0, None
        outcome = {"treasury": "buyback", "blocked": "blocked"}.get(kind, "credited")
        deposit = dict(
            deposit_ref=ref, currency=CURRENCY, tx_hash=tx_hash, sender=address, deposit_address=service,
            amount_raw=amount, decimals=9, block_number=block, usd_price=1.0, observed_at=now,
        )
        if kind == "customer":
            base = g.base_cents(total_raw) - g.base_cents(prev_total)
            worth = base + int(base * g.bonus_pct())
            if worth > 0:
                # What this week's limit still allows, after the gamma that
                # is already waiting (it goes first).
                room = _room(session, now)
                cents = worth if room is None else min(worth, max(0, room - _waiting_total(session)))
                waiting = worth - cents
                if cents > 0:
                    balance_after = _credit(
                        session, owner=owner, cents=cents, ledger_ref=ref, spend_ref=ref, address=address,
                        service=service, amount_raw=amount, block=block, tx_hash=tx_hash, observed_at=now, now=now,
                        part=waiting > 0,
                    )
                else:
                    outcome = "waiting"
            else:
                outcome = "logged"
        elif kind == "blocked":
            session.add(CryptoDepositORM(
                **deposit, status="blocked", matched_by=f"sanctions:{source}"[:32], user_id=owner,
            ))

        session.add(GammaSpendORM(
            spend_id=ref, netuid=netuid, subnet_counter=counter, service_hotkey=service, hotkey=address,
            kind=kind, amount_raw=amount, total_raw=total_raw, block_number=block, tx_hash=tx_hash,
            user_id=owner if kind == "customer" else None, credited_cents=cents if kind == "customer" else None,
            waiting_cents=waiting, coldkey=coldkey, observed_at=now,
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
        return outcome, cents, balance_after, amount, waiting


# --- signed requests ----------------------------------------------------------------


def signed_request_key(session_factory, hotkey: str):
    """The API key a btauth/1 request signed by `hotkey` acts as: one per
    account and hotkey, made on first use, found by a tag only the server
    sets ("<chain>:<address>#btauth"), so no key a user made or named can
    stand in for it. Whatever its row says, it is used with exactly
    SIGNED_REQUEST_SCOPES, never as an admin and never expired. Its secret
    is never shown. Unlinking the hotkey or turning its sign-in off deletes
    it, and every request checks the hotkey still signs in."""
    from greencompute_gateway.transport.errors import coded

    address = _canonical(hotkey)
    if address is None:
        raise coded(401, "signed_request_invalid", {"message": "X-Bittensor-Hotkey isn't a valid hotkey",
                                                     "reason": "malformed"})
    tag = wallet_tag(CHAIN, address) + SIGNED_TAG_SUFFIX

    def as_used(key: APIKeyORM):
        return stored_api_key(key).model_copy(update={
            "scopes": list(g.SIGNED_REQUEST_SCOPES), "admin": False, "expires_at": None,
        })

    with session_scope(session_factory) as session:
        wallet = session.scalar(select(VerifiedWalletORM).where(
            VerifiedWalletORM.chain == CHAIN, VerifiedWalletORM.address == address))
        if wallet is None:
            raise coded(402, "gamma_payment_required", {
                "message": (
                    "This hotkey has no Green Compute account yet. Spend SN110 gamma at our service hotkey "
                    f"{g.service_hotkey()} and the hotkey's account is opened and credited $1 per gamma within "
                    "about a minute, up to our weekly gamma limit (`limit` at GET /platform/gamma says what is "
                    "left this week; gamma above it is credited in a later week); or sign in with the hotkey "
                    "(POST /platform/auth/wallet/challenge)."
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
        find = select(APIKeyORM).where(
            APIKeyORM.user_id == user_id, APIKeyORM.issued_by_wallet == tag,
        ).order_by(APIKeyORM.created_at, APIKeyORM.key_id).limit(1)
        key = session.scalar(find)
        if key is not None:
            return as_used(key)
        # The account's lock, as every key a wallet's sign-in makes takes it,
        # so two first requests can't make two keys or race a revocation.
        user = session.get(UserORM, user_id, with_for_update=True)
        if user is None or (user.profile_metadata or {}).get("deleted_at"):
            raise coded(403, "forbidden", "The account this hotkey signs in to no longer exists. "
                                          "Contact support@green-compute.com.")
        key = session.scalar(find)
        if key is None:
            key = APIKeyORM(
                key_id=str(uuid4()), user_id=user_id, name=g.SIGNED_REQUEST_KEY_NAME, admin=False,
                scopes=list(g.SIGNED_REQUEST_SCOPES), secret=f"gk_{secrets.token_urlsafe(32)}", created_at=_now(),
                issued_by_wallet=tag,
            )
            session.add(key)
            session.flush()
            log.info("gamma: signed-request key %s for account %s (hotkey %s)", key.key_id, user_id, _short(address))
        return as_used(key)


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
        credited, waiting = session.execute(
            select(func.coalesce(func.sum(GammaSpendORM.credited_cents), 0),
                   func.coalesce(func.sum(GammaSpendORM.waiting_cents), 0)).where(
                GammaSpendORM.hotkey == address, GammaSpendORM.kind == "customer",
            )).one()
        credited, waiting = int(credited or 0), int(waiting or 0)
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
        # Spent on chain (public there) but not yet credited: it is waiting
        # for the weekly gamma limit and is credited in a later week.
        "waiting_usd": g.usd_text(waiting),
        "as_of_block": last_block(session_factory),
    }
    if remaining is None:
        out["note"] = ("used_usd and remaining_usd are shown to the hotkey itself: sign this request with it "
                       "(btauth/1), on an account the hotkey signs in to.")
    return out


def spent_totals(session_factory, *, recent: int = 20) -> dict:
    """What has been spent at our service, as the watcher recorded it:
    customers' payments, our buybacks, and spends that were blocked; plus
    the latest buybacks. Summed from the log of spends (what each was when
    it happened), so a hotkey that is later classed differently doesn't
    rewrite history. Anyone can check the sum on chain
    (GammaSpent(netuid, service, *))."""
    netuid = g.netuid()
    with session_scope(session_factory) as session:
        rows = session.execute(
            select(GammaSpendORM.kind, func.coalesce(func.sum(GammaSpendORM.amount_raw), 0),
                   func.count(func.distinct(GammaSpendORM.hotkey)))
            .where(GammaSpendORM.netuid == netuid)
            .group_by(GammaSpendORM.kind)
        ).all()
        buybacks = session.scalars(
            select(GammaSpendORM).where(GammaSpendORM.netuid == netuid, GammaSpendORM.kind == "treasury")
            .order_by(GammaSpendORM.block_number.desc(), GammaSpendORM.observed_at.desc()).limit(recent)
        ).all()
        recent_buybacks = [{
            "gamma": g.gamma_text(b.amount_raw), "block": b.block_number, "tx_hash": b.tx_hash, "hotkey": b.hotkey,
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
    with session_scope(session_factory) as session:
        row = session.get(KeyValueStateORM, CHAIN_STATUS_KEY)
        if row is None or not row.value:
            return None
        try:
            return json.loads(row.value)
        except ValueError:
            return None

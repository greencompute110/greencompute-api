"""Per-API-key and per-account spending, for their optional limits.

A key's spend is every charge on rentals it started (GPU time, matched
through deployments.created_by_key_id) plus every inference request it made
(matched through invocation_records.api_key_id). Saved-pod storage is never
counted against a limit (see _NOT_COUNTED). Shared by the
gateway (checks before starting a rental or serving a request) and the control
plane (stops running rentals once a key reaches its limit). The account's
own limits and a new wallet account's first-week limits are at the end.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from greencompute_persistence.db import session_scope
from greencompute_persistence.orm import APIKeyORM, DeploymentORM, InvocationRecordORM, LedgerEntryORM, UserORM

SPEND_WINDOW = timedelta(hours=24)

# Ledger debits that aren't spending: staff taking money out of an account
# (an adjustment, clawing back a payment, credit forfeited when the account
# closed). The same as billing_monitor.MANUAL_DEBITS (a test keeps them equal).
_NOT_SPEND = ("adjustment", "refund", "chargeback", "account_closed")

# Ledger kinds no spend limit counts: those, and saved-pod storage. A pod a
# limit paused keeps being charged $1/day for its disk; counting that would
# hold the account (or key) at its limit for good (10 saved pods against a
# $10 cap) and block resuming them. Storage is still charged, only not
# counted here.
_NOT_COUNTED = _NOT_SPEND + ("storage",)

# Deployments in these states no longer count towards max_active_rentals.
_FINISHED_STATES = ("terminated", "failed")


def key_spend_cents(session_factory, key_id: str, since: datetime | None = None) -> int:
    """Cents a key has spent since `since` (default: the last 24 hours)."""
    since = since or datetime.now(UTC) - SPEND_WINDOW
    with session_scope(session_factory) as session:
        rentals = session.scalar(
            select(func.coalesce(func.sum(LedgerEntryORM.amount_cents), 0))
            .select_from(LedgerEntryORM)
            .join(DeploymentORM, LedgerEntryORM.reference_id == DeploymentORM.deployment_id)
            .where(
                DeploymentORM.created_by_key_id == key_id,
                LedgerEntryORM.amount_cents < 0,
                LedgerEntryORM.kind.not_in(_NOT_COUNTED),
                LedgerEntryORM.created_at >= since,
            )
        ) or 0
        inference = session.scalar(
            select(func.coalesce(func.sum(LedgerEntryORM.amount_cents), 0))
            .select_from(LedgerEntryORM)
            .join(InvocationRecordORM, LedgerEntryORM.reference_id == InvocationRecordORM.request_id)
            .where(
                InvocationRecordORM.api_key_id == key_id,
                LedgerEntryORM.kind == "inference",
                LedgerEntryORM.amount_cents < 0,
                LedgerEntryORM.created_at >= since,
            )
        ) or 0
    return -int(rentals + inference)


def key_active_rentals(session_factory, key_id: str) -> int:
    """Deployments this key started that haven't finished."""
    with session_scope(session_factory) as session:
        return session.scalar(
            select(func.count())
            .select_from(DeploymentORM)
            .where(
                DeploymentORM.created_by_key_id == key_id,
                DeploymentORM.state.not_in(_FINISHED_STATES),
            )
        ) or 0


def keys_with_spend_limits(session_factory) -> dict[str, int]:
    """key_id -> daily spend limit in cents, for every key that has one."""
    with session_scope(session_factory) as session:
        rows = session.execute(
            select(APIKeyORM.key_id, APIKeyORM.daily_spend_limit_cents).where(
                APIKeyORM.daily_spend_limit_cents.is_not(None)
            )
        ).all()
    return {key_id: int(limit) for key_id, limit in rows}


def key_rental_room(
    session_factory, key_id: str, max_active_rentals: int | None, daily_spend_limit_usd: float | None,
) -> tuple[int | None, str | None, dict]:
    """How many more rentals a key may start now: (room, reason, numbers).
    room None means no limit; 0 with a reason means none ("spend" when its
    daily spend limit is reached, "active" at its limit of rentals at once).
    The one rule behind both the deployments route and fleets."""
    room: int | None = None
    numbers: dict = {}
    if max_active_rentals is not None:
        active = key_active_rentals(session_factory, key_id)
        numbers["active_rentals"] = active
        room = max_active_rentals - active
        if room <= 0:
            return 0, "active", numbers
    if daily_spend_limit_usd is not None:
        spent = key_spend_cents(session_factory, key_id)
        numbers["spent_cents"] = spent
        if spent >= round(daily_spend_limit_usd * 100):
            return 0, "spend", numbers
    return room, None, numbers


# --- New wallet accounts ---------------------------------------------------------
# An account a wallet signature created has no email and nobody has vouched
# for it, which is what abuse looks like too. For its first week (unless the
# website has since confirmed an email for it) the account as a whole, every
# key together, may have only a few rentals running and spend only so much
# a day. Env: GREENCOMPUTE_NEW_WALLET_MAX_RENTALS (2),
# GREENCOMPUTE_NEW_WALLET_DAILY_SPEND_USD (50), GREENCOMPUTE_NEW_WALLET_DAYS (7).


def _env_number(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


def new_account_limits(session_factory, user_id: str | None, now: datetime | None = None) -> dict | None:
    """The limits a new wallet account is under now, or None when it isn't
    one: {"max_active_rentals", "daily_spend_limit_usd", "until"}."""
    if not user_id:
        return None
    with session_scope(session_factory) as session:
        row = session.execute(
            select(UserORM.signup_method, UserORM.email_verified_at, UserORM.created_at).where(UserORM.user_id == user_id)
        ).first()
    if row is None or row.signup_method != "wallet" or row.email_verified_at is not None or row.created_at is None:
        return None
    created = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=UTC)
    until = created + timedelta(days=_env_number("GREENCOMPUTE_NEW_WALLET_DAYS", 7))
    if (now or datetime.now(UTC)) >= until:
        return None
    return {
        "max_active_rentals": int(_env_number("GREENCOMPUTE_NEW_WALLET_MAX_RENTALS", 2)),
        "daily_spend_limit_usd": _env_number("GREENCOMPUTE_NEW_WALLET_DAILY_SPEND_USD", 50),
        "until": until,
    }


def account_spend_cents(session_factory, user_id: str, since: datetime | None = None) -> int:
    """Cents the account spent since `since` (default: the last 24 hours):
    every charge, by any key or none, but saved-pod storage (_NOT_COUNTED)."""
    since = since or datetime.now(UTC) - SPEND_WINDOW
    with session_scope(session_factory) as session:
        total = session.scalar(
            select(func.coalesce(func.sum(LedgerEntryORM.amount_cents), 0)).where(
                LedgerEntryORM.user_id == user_id,
                LedgerEntryORM.amount_cents < 0,
                LedgerEntryORM.kind.not_in(_NOT_COUNTED),
                LedgerEntryORM.created_at >= since,
            )
        ) or 0
    return -int(total)


def account_active_rentals(session_factory, user_id: str) -> int:
    """The account's deployments that haven't finished, whoever started them."""
    with session_scope(session_factory) as session:
        return session.scalar(
            select(func.count()).select_from(DeploymentORM).where(
                DeploymentORM.owner_user_id == user_id,
                DeploymentORM.state.not_in(_FINISHED_STATES),
            )
        ) or 0


def new_account_room(
    session_factory, user_id: str | None, *, starting_rental: bool = True,
) -> tuple[str | None, dict]:
    """(reason, numbers) when a new wallet account is at one of its limits:
    "active" (rentals at once, checked only when `starting_rental`) or
    "spend" (the last 24 hours); (None, {}) otherwise. The numbers include
    the limits and when they end."""
    limits = new_account_limits(session_factory, user_id)
    if limits is None:
        return None, {}
    numbers = dict(limits)
    if starting_rental:
        numbers["active_rentals"] = account_active_rentals(session_factory, user_id)
        if numbers["active_rentals"] >= limits["max_active_rentals"]:
            return "active", numbers
    numbers["spent_cents"] = account_spend_cents(session_factory, user_id)
    if numbers["spent_cents"] >= round(limits["daily_spend_limit_usd"] * 100):
        return "spend", numbers
    return None, numbers


# --- The account's own limits ---------------------------------------------------
# Any customer may set limits for the whole account, every key together
# (request #19: "stop everything at $X"): spend in any 24 hours, counted as
# account_spend_cents counts it, and rentals running at once. Opt-in; NULL is
# no limit. For a new wallet account the stricter of its own setting and the
# first-week default above applies.

#: Where a limit comes from: the customer's own setting, or a new wallet
#: account's first-week default.
OWN, NEW_ACCOUNT = "account", "new_account"


def account_own_limits(session_factory, user_id: str | None) -> dict:
    """The limits the customer set: {"max_active_rentals", "daily_spend_limit_cents"}
    (None: not set; also for an unknown account)."""
    none = {"max_active_rentals": None, "daily_spend_limit_cents": None}
    if not user_id:
        return none
    with session_scope(session_factory) as session:
        row = session.execute(
            select(UserORM.max_active_rentals, UserORM.daily_spend_limit_cents).where(UserORM.user_id == user_id)
        ).first()
    if row is None:
        return none
    return {"max_active_rentals": row.max_active_rentals, "daily_spend_limit_cents": row.daily_spend_limit_cents}


def accounts_with_spend_limits(session_factory) -> dict[str, int]:
    """user_id -> the daily spend limit in cents the customer set, for every
    account that has one."""
    with session_scope(session_factory) as session:
        rows = session.execute(
            select(UserORM.user_id, UserORM.daily_spend_limit_cents).where(
                UserORM.daily_spend_limit_cents.is_not(None)
            )
        ).all()
    return {user_id: int(limit) for user_id, limit in rows}


def stricter(own: int | None, new_account: int | None) -> tuple[int | None, str | None]:
    """(limit, source): the lower of the customer's own limit and the new
    wallet account default. A tie is the default's, since raising one's own
    limit wouldn't lift it."""
    if new_account is not None and (own is None or new_account <= own):
        return new_account, NEW_ACCOUNT
    if own is not None:
        return own, OWN
    return None, None


def account_limits(session_factory, user_id: str | None, now: datetime | None = None) -> dict | None:
    """The account-wide limits in force now, or None when there are none:
    {"max_active_rentals", "max_active_rentals_source", "daily_spend_limit_cents",
    "daily_spend_limit_source", "own", "new_account"}; each `*_source` is
    OWN, NEW_ACCOUNT or None, `own` is account_own_limits and `new_account`
    new_account_limits (or None)."""
    own = account_own_limits(session_factory, user_id)
    new = new_account_limits(session_factory, user_id, now)
    if new is None and own["max_active_rentals"] is None and own["daily_spend_limit_cents"] is None:
        return None
    max_active, max_source = stricter(own["max_active_rentals"], new["max_active_rentals"] if new else None)
    spend, spend_source = stricter(
        own["daily_spend_limit_cents"], round(new["daily_spend_limit_usd"] * 100) if new else None)
    return {
        "max_active_rentals": max_active, "max_active_rentals_source": max_source,
        "daily_spend_limit_cents": spend, "daily_spend_limit_source": spend_source,
        "own": own, "new_account": new,
    }


def account_room(
    session_factory, user_id: str | None, *, starting_rental: bool = True, limits: dict | None = None,
) -> tuple[str | None, str | None, dict]:
    """(reason, source, numbers) when the account is at one of its limits:
    reason "active" (rentals at once, checked only when `starting_rental`) or
    "spend" (the last 24 hours), source OWN or NEW_ACCOUNT; (None, None,
    numbers) otherwise. For a NEW_ACCOUNT source the numbers are what
    new_account_room gives (its limits and `until`); for OWN,
    "max_active_rentals"/"daily_spend_limit_usd" are the customer's."""
    limits = limits if limits is not None else account_limits(session_factory, user_id)
    if limits is None:
        return None, None, {}
    measured: dict = {}

    def numbers_for(source: str) -> dict:
        if source == NEW_ACCOUNT:
            out = dict(limits["new_account"])
        else:
            own = limits["own"]
            cents = own["daily_spend_limit_cents"]
            out = {"max_active_rentals": own["max_active_rentals"],
                   "daily_spend_limit_usd": cents / 100 if cents is not None else None}
        out.update(measured)
        return out

    if starting_rental and limits["max_active_rentals"] is not None:
        measured["active_rentals"] = account_active_rentals(session_factory, user_id)
        if measured["active_rentals"] >= limits["max_active_rentals"]:
            return "active", limits["max_active_rentals_source"], numbers_for(limits["max_active_rentals_source"])
    if limits["daily_spend_limit_cents"] is not None:
        measured["spent_cents"] = account_spend_cents(session_factory, user_id)
        if measured["spent_cents"] >= limits["daily_spend_limit_cents"]:
            return "spend", limits["daily_spend_limit_source"], numbers_for(limits["daily_spend_limit_source"])
    return None, None, measured

"""Per-API-key spending, for the key's optional limits.

A key's spend is every charge on rentals it started (GPU time and saved-pod
storage, matched through deployments.created_by_key_id) plus every inference
request it made (matched through invocation_records.api_key_id). Shared by the
gateway (checks before starting a rental or serving a request) and the control
plane (stops running rentals once a key reaches its limit).
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from greencompute_persistence.db import session_scope
from greencompute_persistence.orm import APIKeyORM, DeploymentORM, InvocationRecordORM, LedgerEntryORM, UserORM

SPEND_WINDOW = timedelta(hours=24)

# Ledger debits that aren't spending: staff clawing back a payment.
_NOT_SPEND = ("refund", "chargeback")

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
    every charge, by any key or none."""
    since = since or datetime.now(UTC) - SPEND_WINDOW
    with session_scope(session_factory) as session:
        total = session.scalar(
            select(func.coalesce(func.sum(LedgerEntryORM.amount_cents), 0)).where(
                LedgerEntryORM.user_id == user_id,
                LedgerEntryORM.amount_cents < 0,
                LedgerEntryORM.kind.not_in(_NOT_SPEND),
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

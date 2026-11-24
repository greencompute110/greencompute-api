"""The network report: what const (Bittensor) asked subnet 110 to publish.

1. GPUs: how many, of what type, and how many each farmer (GPU provider)
   brings, under an anonymous label ("Farmer A").
2. Revenue: what customers paid, per day and week, by card, crypto and gamma.
3. Buybacks: revenue is bought back as SN110 alpha that is burned. A payment
   in gamma already is one (the customer burned alpha to mint it). Card and
   crypto revenue is bought back by the owner minting gamma with that much
   TAO and spending it at our own service, which the gamma watcher records
   (gamma_spends, kind "treasury"). So: to buy back = card + crypto revenue;
   bought back = what the treasury spent; and all of it can be checked on
   chain as GammaSpent at our service hotkey.
4. Farmers are paid in USD on their contract's terms, per monthly statement.

Nothing is public until staff publish it (settings.published): GPU counts
were deliberately not shown on the site before.

What counts as revenue (settings.revenue_basis):
- "paid" (default): money customers paid in. Ledger top-ups that came from a
  real payment (a deposit_ref: card, an on-chain transfer, an invoice staff
  confirmed against a transaction, gamma), without the payment bonus, less
  card chargebacks. Credit staff gave (no deposit_ref) is not revenue.
- "usage": what was charged for compute: rentals, private models, shared
  model requests and saved-disk storage.
Both are always computed; the basis says which one the headline and the
buyback use.

What the public report holds back, so that nobody polling it can read one
customer's payment or how many GPUs are rented right now:
- money and GPU-hours stop at the end of the last completed UTC week (totals
  too); the week in progress and single days are for staff only;
- farmers appear under their labels, never by name, and never with their
  revenue; what farmers were paid shows only when
  settings.show_farmer_payments is on.
Buybacks already made and gamma spent on chain are public on the chain
itself, so those are current.

Staff debits (refund, adjustment) are not netted against revenue: they
correct many different things. Staff see them listed beside the report. Accounts in settings.excluded_user_ids (our own test accounts)
are left out of both.

Machines: a node is listed while it has reported within
settings.listed_within_days (default 30) and isn't hidden; it is online
while it has reported within the node-inventory timeout (15 minutes), the
same rule as the admin GPU usage page.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select

from greencompute_persistence import session_scope
from greencompute_persistence.orm import (
    CryptoInvoiceORM,
    DeploymentORM,
    FarmerORM,
    FarmerPaymentORM,
    GammaSpendORM,
    KeyValueStateORM,
    LedgerEntryORM,
    NodeFarmerORM,
    NodeInventoryORM,
)
from greencompute_gateway.domain import gamma as g

log = logging.getLogger(__name__)

SETTINGS_KEY = "report:settings"
DEFAULT_SETTINGS = {
    "published": False,
    "since": "2026-11-01",
    "revenue_basis": "paid",
    "listed_within_days": 30,
    "excluded_user_ids": [],
    # Whether the public report says what farmers were paid (totals only).
    "show_farmer_payments": False,
}
BASES = {
    "paid": "What customers paid in: card, crypto and gamma payments, without bonuses, less card chargebacks.",
    "usage": "What was charged for compute: GPU rentals, private models, shared model requests and saved disks.",
}
METHODS = (("card", "Card"), ("crypto", "Crypto"), ("gamma", "Gamma"))
UNASSIGNED = "Unassigned"
MAX_WEEKS = 26
_RATE = re.compile(r"\$([\d.]+)/hr")
_MONTH = re.compile(r"20[2-9]\d-(0[1-9]|1[0-2])")
_cache_lock = threading.Lock()
_build_lock = threading.Lock()
_cache: dict = {"at": 0.0, "value": None, "generation": 0}
CACHE_SECONDS = 60


class ReportError(ValueError):
    """A change that can't be made as asked; the message says why."""


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=UTC)


def _usd(cents: int | float) -> str:
    return f"{round(cents) / 100:.2f}"


def _forget_cache() -> None:
    """Drop the cached public report; a build already under way is then
    discarded rather than cached (its generation is old)."""
    with _cache_lock:
        _cache["at"], _cache["value"] = 0.0, None
        _cache["generation"] += 1


# --- settings -----------------------------------------------------------------------


def get_settings(session_factory) -> dict:
    with session_scope(session_factory) as session:
        row = session.get(KeyValueStateORM, SETTINGS_KEY)
        stored = {}
        if row is not None and row.value:
            try:
                stored = json.loads(row.value)
            except ValueError:
                stored = {}
    return {**DEFAULT_SETTINGS, **{k: v for k, v in stored.items() if k in DEFAULT_SETTINGS}}


def update_settings(session_factory, changes: dict) -> dict:
    settings = get_settings(session_factory)
    for key, value in (changes or {}).items():
        if key not in DEFAULT_SETTINGS:
            raise ReportError(f"unknown setting {key!r}; choose from {sorted(DEFAULT_SETTINGS)}")
        if key in ("published", "show_farmer_payments"):
            if not isinstance(value, bool):
                raise ReportError(f"{key} must be true or false")
        elif key == "since":
            try:
                value = date.fromisoformat(str(value)).isoformat()
            except ValueError:
                raise ReportError("since must be a date, YYYY-MM-DD") from None
        elif key == "revenue_basis":
            if value not in BASES:
                raise ReportError(f"revenue_basis must be one of {sorted(BASES)}")
        elif key == "listed_within_days":
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 3650:
                raise ReportError("listed_within_days must be a whole number of days, 1 to 3650")
        elif key == "excluded_user_ids":
            if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
                raise ReportError("excluded_user_ids must be a list of account ids")
            value = sorted({v.strip() for v in value})
        settings[key] = value
    with session_scope(session_factory) as session:
        row = session.get(KeyValueStateORM, SETTINGS_KEY) or KeyValueStateORM(key=SETTINGS_KEY)
        row.value = json.dumps(settings)
        row.updated_at = _now()
        session.add(row)
    _forget_cache()
    return settings


def _since(settings: dict) -> datetime:
    return datetime.combine(date.fromisoformat(settings["since"]), datetime.min.time(), tzinfo=UTC)


# --- farmers and machines -----------------------------------------------------------


def _gpu(model: str | None) -> tuple[str, str]:
    from greencompute_gateway.infrastructure.analytics_repository import _gpu_key, _gpu_label

    key = _gpu_key(model)
    return key, _gpu_label(key)


def _farmer_out(row: FarmerORM) -> dict:
    return {
        "farmer_id": row.farmer_id, "name": row.name, "public_label": row.public_label,
        "terms": _terms(row.terms), "notes": row.notes or "",
        "archived": row.archived_at is not None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _terms(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    return {key: float(raw.get(key) or 0) for key in ("usd_per_gpu_hour", "revenue_share_pct", "monthly_fee_usd")}


def _check_terms(terms) -> dict:
    if terms is None:
        return _terms({})
    if not isinstance(terms, dict):
        raise ReportError("terms must be an object")
    out = {}
    for key, top in (("usd_per_gpu_hour", 100.0), ("revenue_share_pct", 100.0), ("monthly_fee_usd", 10_000_000.0)):
        value = terms.get(key, 0) or 0
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= float(value) <= top:
            raise ReportError(f"terms.{key} must be a number from 0 to {top:g}")
        out[key] = float(value)
    return out


def _next_label(session) -> str:
    taken = set(session.scalars(select(FarmerORM.public_label)).all())
    n = 0
    while True:
        letters, k = "", n
        while True:
            letters = chr(ord("A") + k % 26) + letters
            k = k // 26 - 1
            if k < 0:
                break
        label = f"Farmer {letters}"
        if label not in taken:
            return label
        n += 1


def list_farmers(session_factory) -> list[dict]:
    with session_scope(session_factory) as session:
        rows = session.scalars(select(FarmerORM).order_by(FarmerORM.public_label)).all()
        return [_farmer_out(r) for r in rows]


def _clean(text, field: str, limit: int, *, required: bool = True) -> str:
    if text is not None and not isinstance(text, str):
        raise ReportError(f"{field} must be text")
    value = " ".join((text or "").split())
    if required and not value:
        raise ReportError(f"{field} is required")
    if len(value) > limit:
        raise ReportError(f"{field} may be at most {limit} characters")
    return value


def create_farmer(session_factory, *, name, public_label=None, terms=None, notes=None) -> dict:
    name = _clean(name, "name", 128)
    with session_scope(session_factory) as session:
        label = _clean(public_label, "public_label", 64, required=False) or _next_label(session)
        clash = session.scalar(select(FarmerORM.farmer_id).where(
            (FarmerORM.name == name) | (FarmerORM.public_label == label)))
        if clash:
            raise ReportError("another farmer already has that name or public label")
        row = FarmerORM(farmer_id=str(uuid4()), name=name, public_label=label, terms=_check_terms(terms),
                        notes=str(notes or "")[:4000] or None, created_at=_now())
        session.add(row)
        session.flush()
        out = _farmer_out(row)
    _forget_cache()
    return out


def update_farmer(session_factory, farmer_id: str, changes: dict) -> dict | None:
    with session_scope(session_factory) as session:
        row = session.get(FarmerORM, farmer_id)
        if row is None:
            return None
        for key, value in (changes or {}).items():
            if key == "name":
                row.name = _clean(value, "name", 128)
            elif key == "public_label":
                row.public_label = _clean(value, "public_label", 64)
            elif key == "terms":
                if not isinstance(value, dict):
                    raise ReportError("terms must be an object")
                row.terms = _check_terms({**_terms(row.terms), **value})  # a term left out keeps its value
            elif key == "notes":
                row.notes = str(value or "")[:4000] or None
            elif key == "archived":
                if not isinstance(value, bool):
                    raise ReportError("archived must be true or false")
                row.archived_at = (row.archived_at or _now()) if value else None
            else:
                raise ReportError(f"unknown field {key!r}")
        clash = session.scalar(select(FarmerORM.farmer_id).where(
            FarmerORM.farmer_id != farmer_id,
            (FarmerORM.name == row.name) | (FarmerORM.public_label == row.public_label)))
        if clash:
            raise ReportError("another farmer already has that name or public label")
        session.flush()
        out = _farmer_out(row)
    _forget_cache()
    return out


def assign_machine(session_factory, node_id: str, *, farmer_id="unset", hidden=None) -> dict | None:
    """Set a machine's farmer (None: unassigned) and/or whether it is left
    out of the report. None when there is no such machine."""
    with session_scope(session_factory) as session:
        if session.get(NodeInventoryORM, node_id) is None:
            return None
        row = session.get(NodeFarmerORM, node_id) or NodeFarmerORM(node_id=node_id, hidden=False)
        if farmer_id != "unset":
            if farmer_id is not None and session.get(FarmerORM, farmer_id) is None:
                raise ReportError("no such farmer")
            row.farmer_id = farmer_id
        if hidden is not None:
            if not isinstance(hidden, bool):
                raise ReportError("hidden must be true or false")
            row.hidden = hidden
        row.updated_at = _now()
        session.add(row)
    _forget_cache()
    return next((m for m in machines(session_factory) if m["node_id"] == node_id), None)


def machines(session_factory, *, settings: dict | None = None, now: datetime | None = None) -> list[dict]:
    """Every machine that ever registered, with whose it is and whether it
    is online, listed in the report or hidden."""
    settings = settings or get_settings(session_factory)
    now = now or _now()
    online_within = timedelta(seconds=int(os.environ.get("GREENCOMPUTE_NODE_INVENTORY_TIMEOUT_SECONDS") or 900))
    listed_within = timedelta(days=int(settings["listed_within_days"]))
    out = []
    with session_scope(session_factory) as session:
        owners = {r.node_id: r for r in session.scalars(select(NodeFarmerORM)).all()}
        for node_id, payload, observed_at in session.execute(
            select(NodeInventoryORM.node_id, NodeInventoryORM.payload, NodeInventoryORM.observed_at)
        ).all():
            payload = payload if isinstance(payload, dict) else {}
            key, label = _gpu(payload.get("gpu_model"))
            seen = _aware(observed_at)
            owner = owners.get(node_id)
            hidden = bool(owner.hidden) if owner is not None else False
            recent = seen is not None and now - seen <= listed_within
            out.append({
                "node_id": node_id, "gpu_model": key, "label": label,
                "gpu_count": int(payload.get("gpu_count") or 0),
                "last_seen": seen.isoformat() if seen else None,
                "online": seen is not None and now - seen <= online_within,
                "farmer_id": owner.farmer_id if owner is not None else None,
                "hidden": hidden,
                "listed": recent and not hidden,
            })
    out.sort(key=lambda m: (not m["listed"], m["label"], m["node_id"]))
    return out


def _gpu_summary(listed: list[dict], farmers: dict[str, dict], *, admin: bool) -> dict:
    def tally(rows: list[dict]) -> dict:
        return {
            "gpus": sum(m["gpu_count"] for m in rows),
            "online_gpus": sum(m["gpu_count"] for m in rows if m["online"]),
            "machines": len(rows),
            "machines_online": sum(1 for m in rows if m["online"]),
        }

    def by_model(rows: list[dict]) -> list[dict]:
        models: dict[str, list[dict]] = {}
        for m in rows:
            models.setdefault(m["gpu_model"], []).append(m)
        return [{"gpu_model": key, "label": group[0]["label"], **tally(group)}
                for key, group in sorted(models.items(), key=lambda kv: kv[1][0]["label"])]

    groups: dict[str | None, list[dict]] = {}
    for m in listed:
        groups.setdefault(m["farmer_id"] if m["farmer_id"] in farmers else None, []).append(m)
    by_farmer = []
    for farmer_id, rows in groups.items():
        farmer = farmers.get(farmer_id) if farmer_id else None
        entry = {"farmer": farmer["public_label"] if farmer else UNASSIGNED, **tally(rows), "by_model": by_model(rows)}
        if admin:
            entry["farmer_id"] = farmer_id
            entry["name"] = farmer["name"] if farmer else None
        by_farmer.append(entry)
    by_farmer.sort(key=lambda e: (e["farmer"] == UNASSIGNED, e["farmer"]))
    return {**tally(listed), "by_model": by_model(listed), "by_farmer": by_farmer}


# --- revenue ------------------------------------------------------------------------


def _day_expr(session, column):
    """The UTC day of a timestamp column, in the database's own dialect."""
    if session.get_bind().dialect.name == "postgresql":
        return func.date(func.timezone("UTC", column))
    return func.date(column)


def _as_day(value) -> date:
    return value if isinstance(value, date) and not isinstance(value, datetime) else date.fromisoformat(str(value)[:10])


def _method(deposit_ref: str) -> str:
    head = deposit_ref.split(":", 1)[0].lower()
    return "card" if head == "stripe" else "gamma" if head == "gamma" else "crypto"


def paid_in(session_factory, since: datetime, until: datetime | None = None, excluded: list[str] | None = None):
    """What customers paid in: ({day: {method: cents}}, {user_id: gamma cents}).
    Days are UTC; the second says who paid how much in gamma."""
    from greencompute_persistence.key_limits import _bonus_rates, _top_up_currency

    out: dict[date, dict[str, int]] = {}
    gamma_by_user: dict[str, int] = {}
    with session_scope(session_factory) as session:
        query = (
            select(LedgerEntryORM.amount_cents, LedgerEntryORM.deposit_ref, LedgerEntryORM.kind,
                   LedgerEntryORM.created_at, CryptoInvoiceORM.bonus_pct, LedgerEntryORM.user_id)
            .outerjoin(CryptoInvoiceORM, CryptoInvoiceORM.invoice_id == LedgerEntryORM.reference_id)
            .where(
                LedgerEntryORM.created_at >= since,
                ((LedgerEntryORM.kind == "topup") & (LedgerEntryORM.amount_cents > 0)
                 & LedgerEntryORM.deposit_ref.is_not(None)) | (LedgerEntryORM.kind == "chargeback"),
            )
        )
        if until is not None:
            query = query.where(LedgerEntryORM.created_at < until)
        if excluded:
            query = query.where(LedgerEntryORM.user_id.not_in(excluded))
        rows = session.execute(query).all()
    rates = _bonus_rates() if rows else {}
    for amount, deposit_ref, kind, created_at, invoice_bonus, user_id in rows:
        day = _aware(created_at).astimezone(UTC).date()
        bucket = out.setdefault(day, {})
        if kind == "chargeback":
            bucket["card"] = bucket.get("card", 0) + int(amount)  # negative: money the card network took back
            continue
        method = _method(deposit_ref)
        bonus = 0.0
        if method != "card":
            bonus = invoice_bonus if invoice_bonus is not None else rates.get(_top_up_currency(deposit_ref), 0.0)
        cents = round(amount / (1 + max(0.0, bonus or 0.0)))
        bucket[method] = bucket.get(method, 0) + cents
        if method == "gamma":
            gamma_by_user[user_id] = gamma_by_user.get(user_id, 0) + cents
    return out, gamma_by_user


def compute_delivered(session_factory, since: datetime, until: datetime | None = None,
                      excluded: list[str] | None = None):
    """What was charged for compute, by UTC day: ({day: cents}, {day: GPU-hours},
    {node_id: {"cents", "gpu_hours"}}). GPU-hours come from each rental
    charge's own rate ("… @ $0.40/hr"). A charge belongs to the machine that
    ran it: the node written on the charge, or for charges from before that
    was recorded, the node its rental is on now."""
    cents_by_day: dict[date, int] = {}
    hours_by_day: dict[date, float] = {}
    by_node: dict[str | None, dict] = {}
    with session_scope(session_factory) as session:
        day = _day_expr(session, LedgerEntryORM.created_at)
        node = func.coalesce(LedgerEntryORM.node_id, DeploymentORM.node_id)
        base = [LedgerEntryORM.amount_cents < 0, LedgerEntryORM.created_at >= since]
        if until is not None:
            base.append(LedgerEntryORM.created_at < until)
        if excluded:
            base.append(LedgerEntryORM.user_id.not_in(excluded))
        usage = session.execute(
            select(day, node, LedgerEntryORM.description, func.sum(-LedgerEntryORM.amount_cents))
            .outerjoin(DeploymentORM, DeploymentORM.deployment_id == LedgerEntryORM.reference_id)
            .where(LedgerEntryORM.kind == "usage", *base)
            .group_by(day, node, LedgerEntryORM.description)
        ).all()
        other = session.execute(
            select(day, func.sum(-LedgerEntryORM.amount_cents))
            .where(LedgerEntryORM.kind.in_(("inference", "storage")), *base)
            .group_by(day)
        ).all()
    for raw_day, node_id, description, cents in usage:
        d, cents = _as_day(raw_day), int(cents or 0)
        rate = _RATE.search(description or "")
        hours = cents / 100 / float(rate.group(1)) if rate and float(rate.group(1)) > 0 else 0.0
        cents_by_day[d] = cents_by_day.get(d, 0) + cents
        hours_by_day[d] = hours_by_day.get(d, 0.0) + hours
        entry = by_node.setdefault(node_id, {"cents": 0, "gpu_hours": 0.0})
        entry["cents"] += cents
        entry["gpu_hours"] += hours
    for raw_day, cents in other:
        d = _as_day(raw_day)
        cents_by_day[d] = cents_by_day.get(d, 0) + int(cents or 0)
    return cents_by_day, hours_by_day, by_node


def _used_by(session_factory, user_ids: list[str], since: datetime, until: datetime | None) -> dict[str, int]:
    """Cents of compute each of these accounts was charged in the window."""
    if not user_ids:
        return {}
    with session_scope(session_factory) as session:
        query = (
            select(LedgerEntryORM.user_id, func.sum(-LedgerEntryORM.amount_cents))
            .where(LedgerEntryORM.user_id.in_(user_ids), LedgerEntryORM.amount_cents < 0,
                   LedgerEntryORM.kind.in_(("usage", "inference", "storage")), LedgerEntryORM.created_at >= since)
            .group_by(LedgerEntryORM.user_id)
        )
        if until is not None:
            query = query.where(LedgerEntryORM.created_at < until)
        return {user_id: int(cents or 0) for user_id, cents in session.execute(query).all()}


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _weeks(first: date, last: date) -> list[date]:
    out, week = [], _week_start(last)
    while week >= _week_start(first) and len(out) < MAX_WEEKS:
        out.append(week)
        week -= timedelta(days=7)
    return out


def _revenue(session_factory, settings: dict, now: datetime, *, completed_weeks_only: bool = False) -> dict:
    """Revenue since settings.since. `completed_weeks_only` (the public
    report): everything stops at the end of the last completed UTC week."""
    since = _since(settings)
    excluded = settings["excluded_user_ids"]
    today = now.astimezone(UTC).date()
    until = None
    last_day = today
    if completed_weeks_only:
        monday = _week_start(today)
        until = datetime.combine(monday, datetime.min.time(), tzinfo=UTC)
        last_day = monday - timedelta(days=1)
    paid, gamma_by_user = paid_in(session_factory, since, until, excluded)
    used_cents, used_hours, _ = compute_delivered(session_factory, since, until, excluded)
    basis = settings["revenue_basis"]

    def paid_day(d: date) -> int:
        return sum(paid.get(d, {}).values())

    headline = paid_day if basis == "paid" else (lambda d: used_cents.get(d, 0))
    all_days = sorted(set(paid) | set(used_cents))
    by_method = {m: sum(v.get(m, 0) for v in paid.values()) for m, _ in METHODS}
    weeks = []
    if last_day >= since.date():
        for week in _weeks(since.date(), last_day):
            days = [week + timedelta(days=i) for i in range(7)]
            weeks.append({
                "week_start": week.isoformat(), "week_end": days[-1].isoformat(),
                "usd": _usd(sum(headline(d) for d in days)),
                **{f"{m}_usd": _usd(sum(paid.get(d, {}).get(m, 0) for d in days)) for m, _ in METHODS},
                "compute_usd": _usd(sum(used_cents.get(d, 0) for d in days)),
                "gpu_hours": f"{sum(used_hours.get(d, 0.0) for d in days):.1f}",
            })
    last_days = [today - timedelta(days=i) for i in range(30) if today - timedelta(days=i) >= since.date()]
    total = sum(headline(d) for d in all_days)
    if basis == "paid":
        # A gamma payment was bought back when its customer minted it.
        already = by_method["gamma"]
    else:
        # On the usage basis, the usage that gamma paid for: each account's
        # gamma covers its own charges first, and nobody else's.
        used = _used_by(session_factory, sorted(gamma_by_user), since, until)
        already = sum(min(cents, used.get(user_id, 0)) for user_id, cents in gamma_by_user.items())
    return {
        "basis": basis,
        "basis_text": BASES[basis],
        # The last day counted; None when no day is yet (the public report
        # before its first completed week).
        "through": last_day.isoformat() if last_day >= since.date() else None,
        "total_cents": total,
        "total_usd": _usd(total),
        "paid_in_usd": _usd(sum(by_method.values())),
        "by_method": [{"method": m, "label": label, "usd": _usd(by_method[m])} for m, label in METHODS],
        "by_method_cents": by_method,
        "bought_back_by_customers_cents": min(already, max(0, total)),
        "weeks": weeks,
        "days": [{"day": d.isoformat(), "usd": _usd(headline(d))} for d in last_days],
        "compute_delivered": {
            "usd": _usd(sum(used_cents.values())),
            "gpu_hours": f"{sum(used_hours.values()):.1f}",
        },
    }


# --- buybacks -----------------------------------------------------------------------


def _buybacks(session_factory, settings: dict, revenue: dict) -> dict:
    since = _since(settings)
    netuid = g.netuid()
    with session_scope(session_factory) as session:
        rows = session.scalars(
            select(GammaSpendORM).where(GammaSpendORM.netuid == netuid, GammaSpendORM.kind == "treasury",
                                        GammaSpendORM.observed_at >= since)
            .order_by(GammaSpendORM.block_number.desc(), GammaSpendORM.observed_at.desc())
        ).all()
        log_rows = [{
            "gamma": g.gamma_text(r.amount_raw), "usd": _usd(g.base_cents(r.amount_raw)), "block": r.block_number,
            "tx_hash": r.tx_hash, "observed_at": r.observed_at.isoformat() if r.observed_at else None,
        } for r in rows]
        bought_cents = sum(g.base_cents(r.amount_raw) for r in rows)
        spent_raw = int(session.scalar(select(func.coalesce(func.sum(GammaSpendORM.amount_raw), 0)).where(
            GammaSpendORM.netuid == netuid)) or 0)
    from greencompute_gateway.application.gamma import last_block

    by_customers = revenue["bought_back_by_customers_cents"]
    # Revenue paid in gamma was bought back by the customer; the rest is
    # ours to buy back.
    owed = max(0, revenue["total_cents"] - by_customers)
    return {
        "to_buy_back_cents": owed,
        "to_buy_back_usd": _usd(owed),
        "bought_back_usd": _usd(bought_cents),
        "outstanding_cents": max(0, owed - bought_cents),
        "outstanding_usd": _usd(max(0, owed - bought_cents)),
        "paid_in_gamma_usd": _usd(by_customers),
        "log": log_rows[:50],
        "on_chain": {
            "netuid": netuid,
            "service_hotkey": g.service_hotkey(),
            "service_hotkeys": g.service_hotkeys(),
            "spent_gamma": g.gamma_text(spent_raw),
            "scope": "All gamma ever spent at our service hotkeys: customers' payments, our buybacks and any "
                     "spends we refused to credit. It counts from the first spend, not from the report's start.",
            "as_of_block": last_block(session_factory),
            "how_to_check": "SubtensorModule.GammaSpent(netuid, service hotkey, *) at any finalized block: "
                            "the sum is every gamma payment and buyback made here.",
        },
    }


def buyback(session_factory, *, now: datetime | None = None) -> dict:
    """What is owed, up to now, and what one buyback for it takes: for staff."""
    now = now or _now()
    settings = get_settings(session_factory)
    revenue = _revenue(session_factory, settings, now)
    out = _buybacks(session_factory, settings, revenue)
    tao_usd = None
    try:
        from greencompute_gateway.infrastructure.price_feed import get_price

        tao_usd = float(get_price("tao")) or None
    except Exception as exc:  # noqa: BLE001
        log.warning("buyback: no TAO price (%s)", exc)
    outstanding = out["outstanding_cents"] / 100
    tao = round(outstanding / tao_usd, 4) if tao_usd and outstanding > 0 else None
    netuid, service = g.netuid(), g.service_hotkey()
    return {
        "since": settings["since"],
        "basis": settings["revenue_basis"],
        "revenue_usd": revenue["total_usd"],
        **{k: v for k, v in out.items() if not k.endswith("_cents")},
        "tao_usd": tao_usd,
        "tao_to_mint": f"{tao:.4f}" if tao else None,
        "commands": [
            f"btcli gamma mint --netuid {netuid} --hotkey gc-buyback --amount-tao {tao:.4f}",
            f"btcli gamma spend --netuid {netuid} --service-hotkey {service} --amount-gamma <the gamma the mint gave> "
            "-H gc-buyback",
        ] if tao else [],
        "weeks": [{"week_start": w["week_start"], "week_end": w["week_end"], "revenue_usd": w["usd"],
                   "gamma_usd": w["gamma_usd"]} for w in revenue["weeks"]],
        "note": "Signed by the subnet owner coldkey: mint gamma with the TAO into the buyback hotkey, then spend "
                "it at our service hotkey. The gateway records the spend as a buyback within a minute; it is "
                "never credited to an account. Keep the deposit open until it shows here.",
    }


def adjustments(session_factory, settings: dict | None = None) -> list[dict]:
    """Staff debits since the report's start (refunds, adjustments): they are
    not netted against revenue, so staff see them and can judge."""
    settings = settings or get_settings(session_factory)
    with session_scope(session_factory) as session:
        rows = session.execute(
            select(LedgerEntryORM.created_at, LedgerEntryORM.kind, LedgerEntryORM.amount_cents,
                   LedgerEntryORM.description, LedgerEntryORM.user_id)
            .where(LedgerEntryORM.kind.in_(("refund", "adjustment")), LedgerEntryORM.amount_cents < 0,
                   LedgerEntryORM.created_at >= _since(settings))
            .order_by(LedgerEntryORM.created_at.desc()).limit(100)
        ).all()
    return [{"day": _aware(when).astimezone(UTC).date().isoformat(), "kind": kind, "usd": _usd(-cents),
             "description": description or "", "user_id": user_id} for when, kind, cents, description, user_id in rows]


# --- farmer statements --------------------------------------------------------------


def _month_bounds(month: str) -> tuple[datetime, datetime]:
    if not isinstance(month, str) or not _MONTH.fullmatch(month):
        raise ReportError("month must be YYYY-MM, from 2020-01 on")
    year, mon = int(month[:4]), int(month[5:])
    start = datetime(year, mon, 1, tzinfo=UTC)
    end = datetime(year + (mon == 12), mon % 12 + 1, 1, tzinfo=UTC)
    return start, end


def _payment_out(row: FarmerPaymentORM) -> dict:
    return {
        "payment_id": row.payment_id, "farmer_id": row.farmer_id, "month": row.month,
        "amount_usd": _usd(row.amount_cents), "paid_at": row.paid_at.isoformat() if row.paid_at else None,
        "reference": row.reference or "", "note": row.note or "",
    }


def statement(session_factory, farmer_id: str, month: str) -> dict | None:
    """A farmer's month: GPU-hours rented on their machines, what customers
    were charged for them, what the contract's terms make that, and what has
    been paid. Machines are the farmer's now (a machine moved between
    farmers takes its history with it); a machine left out of the public
    report still earns."""
    start, end = _month_bounds(month)
    with session_scope(session_factory) as session:
        farmer = session.get(FarmerORM, farmer_id)
        if farmer is None:
            return None
        farmer_out = _farmer_out(farmer)
        payments = [_payment_out(p) for p in session.scalars(
            select(FarmerPaymentORM).where(FarmerPaymentORM.farmer_id == farmer_id, FarmerPaymentORM.month == month)
            .order_by(FarmerPaymentORM.paid_at)).all()]
        paid_cents = int(session.scalar(select(func.coalesce(func.sum(FarmerPaymentORM.amount_cents), 0)).where(
            FarmerPaymentORM.farmer_id == farmer_id, FarmerPaymentORM.month == month)) or 0)
    settings = get_settings(session_factory)
    mine = [m for m in machines(session_factory, settings=settings) if m["farmer_id"] == farmer_id]
    # Our own test accounts are left out here as in the report: they pay
    # nothing, so there is no revenue to share on what they ran.
    _, _, by_node = compute_delivered(session_factory, start, end, settings["excluded_user_ids"])
    by_model: dict[str, dict] = {}
    rows = []
    for m in mine:
        usage = by_node.get(m["node_id"], {"cents": 0, "gpu_hours": 0.0})
        rows.append({"node_id": m["node_id"], "label": m["label"], "gpu_count": m["gpu_count"],
                     "gpu_hours": f"{usage['gpu_hours']:.1f}", "revenue_usd": _usd(usage["cents"])})
        model = by_model.setdefault(m["label"], {"label": m["label"], "cents": 0, "gpu_hours": 0.0})
        model["cents"] += usage["cents"]
        model["gpu_hours"] += usage["gpu_hours"]
    revenue_cents = sum(v["cents"] for v in by_model.values())
    gpu_hours = sum(v["gpu_hours"] for v in by_model.values())
    terms = farmer_out["terms"]
    share = revenue_cents * terms["revenue_share_pct"] / 100
    per_hour = gpu_hours * terms["usd_per_gpu_hour"] * 100
    fee = terms["monthly_fee_usd"] * 100
    total = round(share + per_hour + fee)
    return {
        "farmer": farmer_out,
        "month": month,
        "machines": rows,
        "by_model": [{"label": v["label"], "gpu_hours": f"{v['gpu_hours']:.1f}", "revenue_usd": _usd(v["cents"])}
                     for v in sorted(by_model.values(), key=lambda v: v["label"])],
        "gpu_hours": f"{gpu_hours:.1f}",
        "revenue_usd": _usd(revenue_cents),
        "payout": {
            "revenue_share_usd": _usd(share), "per_gpu_hour_usd": _usd(per_hour), "monthly_fee_usd": _usd(fee),
            "total_usd": _usd(total),
        },
        "paid_usd": _usd(paid_cents),
        "balance_usd": _usd(total - paid_cents),
        "payments": payments,
    }


def record_payment(session_factory, farmer_id: str, *, month, amount_usd, reference=None, note=None,
                   paid_at=None, created_by=None) -> dict | None:
    _month_bounds(month)
    if isinstance(amount_usd, bool) or not isinstance(amount_usd, (int, float)) or not 0 < float(amount_usd) <= 1e8:
        raise ReportError("amount_usd must be a number above 0")
    when = _now()
    if paid_at:
        try:
            when = _aware(datetime.fromisoformat(str(paid_at).replace("Z", "+00:00")))
        except ValueError:
            raise ReportError("paid_at must be a date and time, e.g. 2026-12-01T10:00:00Z") from None
    with session_scope(session_factory) as session:
        if session.get(FarmerORM, farmer_id) is None:
            return None
        row = FarmerPaymentORM(
            payment_id=str(uuid4()), farmer_id=farmer_id, month=month, amount_cents=round(float(amount_usd) * 100),
            paid_at=when, reference=_clean(reference, "reference", 256, required=False) or None,
            note=str(note or "")[:2000] or None, created_by=created_by, created_at=_now(),
        )
        session.add(row)
        session.flush()
        out = _payment_out(row)
    _forget_cache()
    return out


def delete_payment(session_factory, payment_id: str) -> bool:
    with session_scope(session_factory) as session:
        row = session.get(FarmerPaymentORM, payment_id)
        if row is None:
            return False
        session.delete(row)
    _forget_cache()
    return True


def _farmer_payments(session_factory, since: datetime) -> dict:
    with session_scope(session_factory) as session:
        rows = session.execute(
            select(FarmerPaymentORM.month, func.sum(FarmerPaymentORM.amount_cents))
            .where(FarmerPaymentORM.month >= since.strftime("%Y-%m"))
            .group_by(FarmerPaymentORM.month).order_by(FarmerPaymentORM.month.desc())
        ).all()
    return {"total_usd": _usd(sum(int(c or 0) for _, c in rows)),
            "months": [{"month": m, "usd": _usd(int(c or 0))} for m, c in rows]}


# --- the report ---------------------------------------------------------------------


def build(session_factory, *, admin: bool = False, now: datetime | None = None) -> dict:
    """The report. `admin`: for staff, with farmers' names and ids, the week
    in progress and single days; otherwise what the public gets."""
    now = now or _now()
    settings = get_settings(session_factory)
    farmers = {f["farmer_id"]: f for f in list_farmers(session_factory)}
    all_machines = machines(session_factory, settings=settings, now=now)
    revenue = _revenue(session_factory, settings, now, completed_weeks_only=not admin)
    buybacks = _buybacks(session_factory, settings, revenue)

    def public(d: dict) -> dict:
        return {k: v for k, v in d.items() if not k.endswith("_cents")}

    out = {
        "generated_at": now.isoformat(timespec="seconds"),
        "netuid": g.netuid(),
        "published": bool(settings["published"]),
        "since": settings["since"],
        "gpus": {**_gpu_summary([m for m in all_machines if m["listed"]], farmers, admin=admin),
                 "listed_within_days": settings["listed_within_days"]},
        "revenue": public(revenue),
        "buybacks": public(buybacks),
    }
    if not admin:
        out["revenue"].pop("days", None)
    if admin or settings["show_farmer_payments"]:
        out["farmer_payments"] = _farmer_payments(session_factory, _since(settings))
    return out


def public_report(session_factory) -> dict | None:
    """The published report, at most a minute old; None until published.
    Whether it is published is asked on every request, so unpublishing takes
    it down at once; one request builds while the others wait for it."""
    if not get_settings(session_factory)["published"]:
        return None

    def cached():
        if _cache["value"] is not None and time.monotonic() - _cache["at"] < CACHE_SECONDS:
            return _cache["value"]
        return None

    with _cache_lock:
        value = cached()
    if value is not None:
        return value
    with _build_lock:
        with _cache_lock:
            value = cached()
            generation = _cache["generation"]
        if value is not None:
            return value
        value = build(session_factory)
        with _cache_lock:
            if _cache["generation"] == generation:  # nothing changed while it was built
                _cache["at"], _cache["value"] = time.monotonic(), value
                return value
    # A setting, a farmer or a machine changed meanwhile: answer from now.
    return public_report(session_factory)

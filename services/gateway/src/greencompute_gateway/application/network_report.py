"""The network report: what const (Bittensor) asked subnet 110 to publish,
shown the way the subnet's owner wants it shown.

1. GPUs: what the network has, by type and by farmer (GPU provider, under
   an anonymous label, "Farmer A"). Two kinds of capacity: machines on the
   on-demand marketplace, which report to the network (measured), and
   machines rented under a contract (bare metal, by the month), which staff
   record from the signed contract (declared).
2. Revenue, per period (settings.period: quarter by default), in four
   streams: gamma, contracts, card and crypto. Staff see it as two flows:
   the normal flow (what came in through the website, each currency on its
   own: card, USDC, USDT, TAO, alpha, and gamma apart from them) and
   contracts (direct deals the sales team makes, for a month, two months, a
   quarter and so on).
3. Buybacks: revenue is bought back as SN110 alpha that is burned. A payment
   in gamma already is one (the customer burned alpha to mint it). The rest
   (contracts, card, crypto) is bought back by the owner after each period
   ends: mint gamma with that much TAO and spend it at our own service,
   which the gamma watcher records (gamma_spends, kind "treasury"). So: to
   buy back = the non-gamma revenue of completed periods; bought back =
   what the treasury spent; and all of it can be checked on chain as
   GammaSpent at our service hotkey.
4. Farmers are paid in USD on their contract's terms, per monthly statement.

Nothing is public until staff publish it (settings.published).

How much the public report says about GPUs (the owner's call: enough to
show the network has the GPUs people want, not a figure to divide revenue
by):
- settings.gpu_detail "bands" (default): counts rounded DOWN to a band
  ("20+"), never up; farmers by label with their GPU types and a share of
  capacity rounded down to 5% ("50%+"), no counts. "exact": the real
  counts, as staff see.
- settings.show_providers: whether farmers are listed at all.
- settings.show_gpu_hours (default off): GPU-hours and compute charged.
A farmer's share is its share of capacity valued at the list rental price
of each GPU (protocol GPU_RATE_CENTS_PER_HOUR; a type with no list price
is valued at its contract's own rate): what an incentive by GPU capacity
would follow.

What counts as revenue (settings.revenue_basis):
- "paid" (default): money customers paid in. Ledger top-ups that came from a
  real payment (a deposit_ref: card, an on-chain transfer, an invoice staff
  confirmed against a transaction, gamma), without the payment bonus, less
  card chargebacks. Credit staff gave (no deposit_ref) is not revenue.
- "usage": what was charged for compute: rentals, private models, shared
  model requests and saved-disk storage.
Both are always computed; the basis says which one the headline and the
buyback use. Contract revenue is the payments staff recorded, on the day
each was received, on either basis. Gamma counts when it is credited: gamma
that is waiting for the weekly gamma limit (application/gamma.py) is revenue
in the week it is credited.

Anyone may see the revenue once the report is published: the two flows,
the normal flow by currency, and the contracts (settings.show_contracts)
under labels ("Contract A") with their term, GPU type and what was
received, never a customer's name or what the contract is worth.

What the public report holds back, so that nobody polling it can read a
payment the moment it lands or how many GPUs are rented right now:
- money stops at the end of the last completed week (totals too), or of
  yesterday, or of the last completed period: settings.public_through.
  Today's figures and single days are for staff only;
- farmers appear under their labels, never by name, and never with their
  revenue; what farmers were paid shows only when
  settings.show_farmer_payments is on, and then only for statement months
  that ended before the period in progress; contracts are never listed,
  only their total per period.
Buybacks already made and gamma spent on chain are public on the chain
itself, so those are current.

Staff debits (refund, adjustment) are not netted against revenue: they
correct many different things. Staff see them listed beside the report.
Accounts in settings.excluded_user_ids (our own test accounts) are left out
of both.

Machines: a node is listed while it has reported within
settings.listed_within_days (default 30) and isn't hidden; it is online
while it has reported within the node-inventory timeout (15 minutes), the
same rule as the admin GPU usage page.
"""
from __future__ import annotations

import calendar
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
    ContractORM,
    ContractPaymentORM,
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
    # What money is reported by, and when a buyback falls due: after each
    # completed week, month or quarter.
    "period": "quarter",
    # Public GPU counts: "bands" (rounded down, "20+") or "exact".
    "gpu_detail": "bands",
    # Whether the public report lists farmers (labels, types, share).
    "show_providers": True,
    # Whether the public report shows GPU-hours and compute charged.
    "show_gpu_hours": False,
    # How fresh the public money figures are: through the last completed
    # "week" (default), through yesterday ("day"), or completed periods only
    # ("period"). Buybacks fall due per `period` whichever this is.
    "public_through": "week",
    # Whether the public report lists the contracts (a label, the term, the
    # GPU type, what was received): never a customer's name or the value.
    "show_contracts": True,
    "listed_within_days": 30,
    "excluded_user_ids": [],
    # Whether the public report says what farmers were paid (totals only).
    "show_farmer_payments": False,
}
PERIODS = ("week", "month", "quarter")
# Periods listed at most, newest first: half a year of weeks, two years of
# months, three years of quarters.
MAX_PERIODS = {"week": 26, "month": 24, "quarter": 12}
GPU_DETAILS = ("bands", "exact")
PUBLIC_THROUGH = ("week", "day", "period")
BASES = {
    "paid": "What customers paid: gamma, contract, card and crypto payments, without bonuses, less card "
            "chargebacks.",
    "usage": "What was charged for compute (GPU rentals, private models, shared model requests and saved disks), "
             "plus contract payments received.",
}
METHODS = (("gamma", "Gamma"), ("contract", "Contracts"), ("card", "Card"), ("crypto", "Crypto"))
# What came in through the website ("normal flow"), by what it was paid in:
# staff see each on its own. Gamma is the odd one out: paid on chain and
# burned by the customer, it brings no cash and needs no buyback.
CURRENCIES = (("card", "Card"), ("usdc", "USDC"), ("usdt", "USDT"), ("tao", "TAO"), ("alpha", "Alpha"),
              ("gamma", "Gamma"), ("other", "Other"))
# The currencies the "crypto" stream is made of.
CRYPTO = ("usdc", "usdt", "tao", "alpha", "other")
UNASSIGNED = "Unassigned"
# Hours in an average month: a contract's monthly value as an hourly rate.
HOURS_PER_MONTH = 730
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
        if key in ("published", "show_farmer_payments", "show_providers", "show_gpu_hours", "show_contracts"):
            if not isinstance(value, bool):
                raise ReportError(f"{key} must be true or false")
        elif key == "period":
            if value not in PERIODS:
                raise ReportError(f"period must be one of {list(PERIODS)}")
        elif key == "gpu_detail":
            if value not in GPU_DETAILS:
                raise ReportError(f"gpu_detail must be one of {list(GPU_DETAILS)}")
        elif key == "public_through":
            if value not in PUBLIC_THROUGH:
                raise ReportError(f"public_through must be one of {list(PUBLIC_THROUGH)}")
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


def _letters(n: int) -> str:
    """0 -> "A", 25 -> "Z", 26 -> "AA": letters to label things with."""
    letters = ""
    while True:
        letters = chr(ord("A") + n % 26) + letters
        n = n // 26 - 1
        if n < 0:
            return letters


def _next_label(session) -> str:
    taken = set(session.scalars(select(FarmerORM.public_label)).all())
    n = 0
    while True:
        label = f"Farmer {_letters(n)}"
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


def _rate_cents(gpu_model: str, fallback: float = 0.0) -> float:
    """What one GPU of this type rents for per hour at list price, in cents;
    `fallback` for a type we don't list (a contract's own rate)."""
    from greencompute_protocol import GPU_RATE_CENTS_PER_HOUR

    rate = GPU_RATE_CENTS_PER_HOUR.get(gpu_model)
    return float(rate) if rate is not None else float(fallback or 0.0)


def _gpu_summary(listed: list[dict], contracted: list[dict], farmers: dict[str, dict], *, admin: bool) -> dict:
    """Counts of on-demand machines (`listed`) and GPUs under contract
    (`contracted`, from contract_capacity), in total, by type and by farmer,
    with each farmer's share of capacity valued at list rental price."""
    def tally(rows: list[dict], under: list[dict]) -> dict:
        gpus = sum(m["gpu_count"] for m in rows)
        contract_gpus = sum(c["gpu_count"] for c in under)
        return {
            "gpus": gpus,
            "online_gpus": sum(m["gpu_count"] for m in rows if m["online"]),
            "machines": len(rows),
            "machines_online": sum(1 for m in rows if m["online"]),
            "contract_gpus": contract_gpus,
            "total_gpus": gpus + contract_gpus,
        }

    def by_model(rows: list[dict], under: list[dict]) -> list[dict]:
        models: dict[str, tuple[str, list[dict], list[dict]]] = {}
        for m in rows:
            models.setdefault(m["gpu_model"], (m["label"], [], []))[1].append(m)
        for c in under:
            models.setdefault(c["gpu_model"], (c["label"], [], []))[2].append(c)
        return [{"gpu_model": key, "label": label, **tally(mine, theirs)}
                for key, (label, mine, theirs) in sorted(models.items(), key=lambda kv: kv[1][0])]

    def value(rows: list[dict], under: list[dict]) -> float:
        """Cents an hour at list price."""
        return (sum(m["gpu_count"] * _rate_cents(m["gpu_model"]) for m in rows)
                + sum(c["gpu_count"] * _rate_cents(c["gpu_model"], c["rate_cents"]) for c in under))

    def owner(row: dict) -> str | None:
        return row["farmer_id"] if row["farmer_id"] in farmers else None

    groups: dict[str | None, tuple[list[dict], list[dict]]] = {}
    for m in listed:
        groups.setdefault(owner(m), ([], []))[0].append(m)
    for c in contracted:
        groups.setdefault(owner(c), ([], []))[1].append(c)
    total = tally(listed, contracted)
    total_value = value(listed, contracted)
    by_farmer = []
    for farmer_id, (rows, under) in groups.items():
        farmer = farmers.get(farmer_id) if farmer_id else None
        counts = tally(rows, under)
        worth = value(rows, under)
        # By value where anything has a price; by GPU count otherwise.
        share = (worth / total_value if total_value > 0
                 else counts["total_gpus"] / total["total_gpus"] if total["total_gpus"] else 0.0)
        entry = {"farmer": farmer["public_label"] if farmer else UNASSIGNED, **counts,
                 "share_pct": round(share * 100, 1), "by_model": by_model(rows, under),
                 "_share": share * 100}  # unrounded, for the public band; never sent
        if admin:
            entry["farmer_id"] = farmer_id
            entry["name"] = farmer["name"] if farmer else None
            entry["usd_per_hour"] = _usd(worth)
        by_farmer.append(entry)
    by_farmer.sort(key=lambda e: (e["farmer"] == UNASSIGNED, e["farmer"]))
    return {**total, "by_model": by_model(listed, contracted), "by_farmer": by_farmer}


def _band(n: int) -> str:
    """A count rounded DOWN to a band, never up: 3 -> "1+", 8 -> "5+",
    24 -> "20+", 172 -> "150+", 2300 -> "2,000+"."""
    n = int(n)
    if n <= 0:
        return "0"
    step = 1 if n < 5 else 5 if n < 10 else 10 if n < 100 else 50 if n < 1000 else 500
    return f"{1 if n < 5 else n // step * step:,}+"


def _share_band(pct: float, *, alone: bool) -> tuple[int, str]:
    """A share rounded DOWN to a step of 5%, like the counts: never more
    than the real share, so the shares shown never add up past 100%.
    (bar width, words): 53.3 -> (50, "50%+"), 3 -> (0, "under 5%")."""
    if alone or pct >= 100:
        return 100, "100%"
    floor = int(max(0.0, pct) // 5) * 5
    if floor == 0:
        return 0, "under 5%" if pct > 0 else "0%"
    return floor, f"{floor}%+"


def _gpu_bands(summary: dict, *, show_providers: bool) -> dict:
    """The summary as the public gets it by default: bands, no counts."""
    with_gpus = [f for f in summary["by_farmer"] if f["total_gpus"] > 0]
    providers = []
    for f in with_gpus if show_providers else []:
        width, words = _share_band(f["_share"], alone=len(with_gpus) == 1)
        providers.append({
            "provider": f["farmer"],
            "types": [m["label"] for m in f["by_model"] if m["total_gpus"] > 0],
            "share_pct": width, "share": words,
        })
    return {
        "detail": "bands",
        "total": _band(summary["total_gpus"]),
        "on_demand": _band(summary["gpus"]),
        "online": _band(summary["online_gpus"]),
        "under_contract": _band(summary["contract_gpus"]) if summary["contract_gpus"] else None,
        "by_model": [{"gpu_model": m["gpu_model"], "label": m["label"], "gpus": _band(m["total_gpus"])}
                     for m in summary["by_model"] if m["total_gpus"] > 0],
        "providers": providers,
        # How many named providers there are; not said when they aren't listed.
        "provider_count": sum(1 for f in with_gpus if f["farmer"] != UNASSIGNED) if show_providers else None,
    }


# --- contracts ----------------------------------------------------------------------


def _as_date(value, field: str, *, required: bool = True) -> date | None:
    if value in (None, ""):
        if required:
            raise ReportError(f"{field} is required: a date, YYYY-MM-DD")
        return None
    try:
        day = date.fromisoformat(str(value))
    except ValueError:
        raise ReportError(f"{field} must be a date, YYYY-MM-DD") from None
    if not date(2020, 1, 1) <= day <= date(2100, 1, 1):
        raise ReportError(f"{field} must be between 2020 and 2100")
    return day


def _add_months(day: date, months: int) -> date:
    """The same day `months` later, or that month's last day when it has no
    such day (31 January + 1 month = 28 or 29 February)."""
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _term_end(start: date, months: int) -> date:
    """The last day of a term of whole months: 1 November + 1 month ends on
    30 November."""
    return _add_months(start, months) - timedelta(days=1)


def _term_months(start: date, end: date | None) -> int | None:
    """How many whole months a contract runs; None when it is open-ended or
    its dates aren't a whole number of months."""
    if end is None or end < start:
        return None
    guess = (end.year - start.year) * 12 + (end.month - start.month)
    for months in (guess, guess + 1):
        if months >= 1 and _term_end(start, months) == end:
            return months
    return None


def _contract_active(row: ContractORM, today: date) -> bool:
    return (row.archived_at is None and row.starts_on <= today
            and (row.ends_on is None or row.ends_on >= today))


def _contract_payment_out(row: ContractPaymentORM) -> dict:
    return {"payment_id": row.payment_id, "contract_id": row.contract_id,
            "received_on": row.received_on.isoformat(), "amount_usd": _usd(row.amount_cents),
            "reference": row.reference or "", "note": row.note or ""}


def _contract_out(row: ContractORM, payments: list[ContractPaymentORM], today: date) -> dict:
    received = sum(p.amount_cents for p in payments)
    months = _term_months(row.starts_on, row.ends_on)
    # What the whole term is worth, when both the monthly value and a term
    # of whole months are known.
    value = row.monthly_cents * months if row.monthly_cents is not None and months else None
    return {
        "contract_id": row.contract_id, "name": row.name, "farmer_id": row.farmer_id,
        "gpu_model": row.gpu_model or "", "gpu_label": _gpu(row.gpu_model)[1] if row.gpu_model else "",
        "gpu_count": int(row.gpu_count or 0),
        "monthly_usd": _usd(row.monthly_cents) if row.monthly_cents is not None else None,
        "starts_on": row.starts_on.isoformat(), "ends_on": row.ends_on.isoformat() if row.ends_on else None,
        "term_months": months,
        "value_usd": _usd(value) if value is not None else None,
        "outstanding_usd": _usd(max(0, value - received)) if value is not None else None,
        "notes": row.notes or "", "archived": row.archived_at is not None,
        "active": _contract_active(row, today),
        "received_usd": _usd(received),
        "payments": [_contract_payment_out(p) for p in payments],
    }


def _one_contract(session, row: ContractORM, today: date) -> dict:
    payments = session.scalars(
        select(ContractPaymentORM).where(ContractPaymentORM.contract_id == row.contract_id)
        .order_by(ContractPaymentORM.received_on.desc(), ContractPaymentORM.created_at.desc())).all()
    return _contract_out(row, list(payments), today)


def list_contracts(session_factory, *, today: date | None = None) -> list[dict]:
    """Every contract with its payments, newest payment first: for staff."""
    today = today or _now().date()
    with session_scope(session_factory) as session:
        rows = session.scalars(select(ContractORM).order_by(ContractORM.archived_at.is_not(None), ContractORM.name)).all()
        return [_one_contract(session, row, today) for row in rows]


def _contract_fields(session, values: dict, *, current: ContractORM | None = None) -> dict:
    """Checked column values for the fields present in `values`."""
    out: dict = {}
    for key, value in values.items():
        if key == "name":
            out["name"] = _clean(value, "name", 128)
        elif key == "farmer_id":
            if value in (None, ""):
                out["farmer_id"] = None
            elif not isinstance(value, str) or session.get(FarmerORM, value) is None:
                raise ReportError("no such farmer")
            else:
                out["farmer_id"] = value
        elif key == "gpu_model":
            text = _clean(value, "gpu_model", 64, required=False)
            out["gpu_model"] = _gpu(text)[0] if text else ""
        elif key == "gpu_count":
            value = 0 if value is None else value
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1_000_000:
                raise ReportError("gpu_count must be a whole number, 0 or more")
            out["gpu_count"] = value
        elif key == "monthly_usd":
            if value is None:
                out["monthly_cents"] = None
            elif isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= float(value) <= 1e9:
                raise ReportError("monthly_usd must be a number, 0 or more, or null")
            else:
                out["monthly_cents"] = round(float(value) * 100)
        elif key == "starts_on":
            out["starts_on"] = _as_date(value, "starts_on")
        elif key == "ends_on":
            out["ends_on"] = _as_date(value, "ends_on", required=False)
        elif key == "term_months":
            # The term in whole months ("one month, two months, a quarter"):
            # the end date follows from the start (_apply_term).
            if "ends_on" in values:
                raise ReportError("send term_months or ends_on, not both")
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 120:
                raise ReportError("term_months must be a whole number of months, 1 to 120")
            out["term_months"] = value
        elif key == "notes":
            out["notes"] = str(value or "")[:4000] or None
        elif key == "archived":
            if not isinstance(value, bool):
                raise ReportError("archived must be true or false")
            out["archived_at"] = ((current.archived_at if current is not None else None) or _now()) if value else None
        else:
            raise ReportError(f"unknown field {key!r}")
    return out


def _apply_term(row: ContractORM, fields: dict) -> None:
    """Set the fields on the contract; a term in months sets its end date
    from its (new) start date."""
    months = fields.pop("term_months", None)
    for key, value in fields.items():
        setattr(row, key, value)
    if months is not None:
        row.ends_on = _term_end(row.starts_on, months)


def _check_contract(row: ContractORM) -> None:
    if row.ends_on is not None and row.ends_on < row.starts_on:
        raise ReportError("ends_on can't be before starts_on")
    if row.gpu_count and not row.gpu_model:
        raise ReportError("say which GPU the contract is for (gpu_model), or set gpu_count to 0")


def create_contract(session_factory, values: dict) -> dict:
    values = dict(values or {})
    values.pop("archived", None)
    for required in ("name", "starts_on"):
        values.setdefault(required, None)
    with session_scope(session_factory) as session:
        fields = _contract_fields(session, values)
        if session.scalar(select(ContractORM.contract_id).where(ContractORM.name == fields["name"])):
            raise ReportError("another contract already has that name")
        months = fields.pop("term_months", None)
        row = ContractORM(**{"contract_id": str(uuid4()), "gpu_model": "", "gpu_count": 0, "created_at": _now(),
                             **fields})
        _apply_term(row, {} if months is None else {"term_months": months})
        _check_contract(row)
        session.add(row)
        session.flush()
        out = _one_contract(session, row, _now().date())
    _forget_cache()
    return out


def update_contract(session_factory, contract_id: str, changes: dict) -> dict | None:
    with session_scope(session_factory) as session:
        row = session.get(ContractORM, contract_id)
        if row is None:
            return None
        _apply_term(row, _contract_fields(session, dict(changes or {}), current=row))
        _check_contract(row)
        if session.scalar(select(ContractORM.contract_id).where(
                ContractORM.contract_id != contract_id, ContractORM.name == row.name)):
            raise ReportError("another contract already has that name")
        session.flush()
        out = _one_contract(session, row, _now().date())
    _forget_cache()
    return out


def _received_on(value) -> date:
    day = _as_date(value, "received_on")
    if day > _now().date() + timedelta(days=1):
        raise ReportError("received_on can't be in the future: record a payment once it has arrived")
    return day


def _payment_cents(amount_usd) -> int:
    if isinstance(amount_usd, bool) or not isinstance(amount_usd, (int, float)) or not 0 < float(amount_usd) <= 1e8:
        raise ReportError("amount_usd must be a number above 0")
    return round(float(amount_usd) * 100)


def update_contract_payment(session_factory, payment_id: str, changes: dict) -> dict | None:
    """Correct a recorded payment: its day, amount, reference or note."""
    with session_scope(session_factory) as session:
        row = session.get(ContractPaymentORM, payment_id)
        if row is None:
            return None
        for key, value in dict(changes or {}).items():
            if key == "received_on":
                row.received_on = _received_on(value)
            elif key == "amount_usd":
                row.amount_cents = _payment_cents(value)
            elif key == "reference":
                row.reference = _clean(value, "reference", 256, required=False) or None
            elif key == "note":
                row.note = str(value or "")[:2000] or None
            else:
                raise ReportError(f"unknown field {key!r}")
        session.flush()
        out = _contract_payment_out(row)
    _forget_cache()
    return out


def record_contract_payment(session_factory, contract_id: str, *, received_on, amount_usd, reference=None,
                            note=None, created_by=None) -> dict | None:
    """Money received on a contract: revenue on `received_on` (a UTC day,
    today at the latest)."""
    day = _received_on(received_on)
    cents = _payment_cents(amount_usd)
    with session_scope(session_factory) as session:
        if session.get(ContractORM, contract_id) is None:
            return None
        row = ContractPaymentORM(
            payment_id=str(uuid4()), contract_id=contract_id, received_on=day,
            amount_cents=cents,
            reference=_clean(reference, "reference", 256, required=False) or None,
            note=str(note or "")[:2000] or None, created_by=created_by, created_at=_now(),
        )
        session.add(row)
        session.flush()
        out = _contract_payment_out(row)
    _forget_cache()
    return out


def delete_contract_payment(session_factory, payment_id: str) -> bool:
    with session_scope(session_factory) as session:
        row = session.get(ContractPaymentORM, payment_id)
        if row is None:
            return False
        session.delete(row)
    _forget_cache()
    return True


def contract_capacity(session_factory, today: date) -> list[dict]:
    """GPUs under an active contract: [{farmer_id, gpu_model, label,
    gpu_count, rate_cents}]. `rate_cents` is the contract's own price per
    GPU-hour, used to value a GPU type we don't list a price for."""
    out = []
    with session_scope(session_factory) as session:
        for row in session.scalars(select(ContractORM).where(ContractORM.gpu_count > 0)).all():
            if not row.gpu_model or not _contract_active(row, today):
                continue
            key, label = _gpu(row.gpu_model)
            rate = (row.monthly_cents / row.gpu_count / HOURS_PER_MONTH) if row.monthly_cents else 0.0
            out.append({"farmer_id": row.farmer_id, "gpu_model": key, "label": label,
                        "gpu_count": int(row.gpu_count), "rate_cents": rate})
    return out


def public_contracts(session_factory, settings: dict, *, through: date | None, today: date) -> list[dict]:
    """The contracts as anyone may see them: a label ("Contract A", by the
    order they were entered, so it never changes), the term, whether it is
    running, the GPU type (with a count as exact as settings.gpu_detail
    allows) and what was received from the report's start through
    `through`. Never the customer's name, the notes or what the contract is
    worth. A contract shows once it has started on or before `through`, and
    while it is running or has brought in money in that time."""
    if through is None:
        return []
    since = _since(settings).date()
    bands = settings["gpu_detail"] == "bands"
    with session_scope(session_factory) as session:
        rows = session.scalars(select(ContractORM).order_by(ContractORM.created_at, ContractORM.contract_id)).all()
        received = {cid: int(cents or 0) for cid, cents in session.execute(
            select(ContractPaymentORM.contract_id, func.sum(ContractPaymentORM.amount_cents))
            .where(ContractPaymentORM.received_on >= since, ContractPaymentORM.received_on <= through)
            .group_by(ContractPaymentORM.contract_id)).all()}
        out = []
        for index, row in enumerate(rows):
            got = received.get(row.contract_id, 0)
            running = _contract_active(row, today)
            if row.starts_on > through or not (running or got):
                continue
            count = int(row.gpu_count or 0) if row.gpu_model else 0
            out.append({
                "label": f"Contract {_letters(index)}",
                "term_months": _term_months(row.starts_on, row.ends_on),
                "starts_on": row.starts_on.isoformat(),
                "ends_on": row.ends_on.isoformat() if row.ends_on else None,
                "status": "active" if running else "ended",
                "gpu_label": _gpu(row.gpu_model)[1] if row.gpu_model else "",
                "gpus": (_band(count) if bands else count) if count else None,
                "received_usd": _usd(got),
            })
    return out


def contract_paid(session_factory, since: date, until: date | None = None) -> dict[date, int]:
    """Cents received on contracts, by day, from `since` to before `until`."""
    with session_scope(session_factory) as session:
        query = (select(ContractPaymentORM.received_on, func.sum(ContractPaymentORM.amount_cents))
                 .where(ContractPaymentORM.received_on >= since).group_by(ContractPaymentORM.received_on))
        if until is not None:
            query = query.where(ContractPaymentORM.received_on < until)
        return {_as_day(day): int(cents or 0) for day, cents in session.execute(query).all()}


# --- revenue ------------------------------------------------------------------------


def _day_expr(session, column):
    """The UTC day of a timestamp column, in the database's own dialect."""
    if session.get_bind().dialect.name == "postgresql":
        return func.date(func.timezone("UTC", column))
    return func.date(column)


def _as_day(value) -> date:
    return value if isinstance(value, date) and not isinstance(value, datetime) else date.fromisoformat(str(value)[:10])


def _currency(deposit_ref: str) -> str:
    """What a payment was made in, from its ledger deposit_ref: card, usdc,
    usdt, tao, alpha or gamma; "other" for a crypto we don't name."""
    from greencompute_persistence.key_limits import _top_up_currency

    if deposit_ref.split(":", 1)[0].lower() == "stripe":
        return "card"
    currency = _top_up_currency(deposit_ref)
    return currency if currency in ("usdc", "usdt", "tao", "alpha", "gamma") else "other"


def _stream(bucket: dict, method: str) -> int:
    """A day's cents for one of the four streams, from its cents by currency."""
    if method == "crypto":
        return sum(bucket.get(c, 0) for c in CRYPTO)
    return bucket.get(method, 0)


def paid_in(session_factory, since: datetime, until: datetime | None = None, excluded: list[str] | None = None):
    """What customers paid in: ({day: {currency: cents}}, {user_id: gamma
    cents}). Currencies are CURRENCIES' (card, usdc, usdt, tao, alpha,
    gamma, other). Days are UTC; the second says who paid how much in gamma."""
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
        currency = _currency(deposit_ref)
        bonus = 0.0
        if currency != "card":
            bonus = invoice_bonus if invoice_bonus is not None else rates.get(_top_up_currency(deposit_ref), 0.0)
        cents = round(amount / (1 + max(0.0, bonus or 0.0)))
        bucket[currency] = bucket.get(currency, 0) + cents
        if currency == "gamma":
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


def _period_start(day: date, period: str) -> date:
    """The first day of `day`'s week (Monday), month or calendar quarter."""
    if period == "week":
        return day - timedelta(days=day.weekday())
    if period == "month":
        return day.replace(day=1)
    return date(day.year, 3 * ((day.month - 1) // 3) + 1, 1)


def _period_next(start: date, period: str) -> date:
    """The first day of the period after the one starting on `start`."""
    if period == "week":
        return start + timedelta(days=7)
    month = start.month - 1 + (1 if period == "month" else 3)
    return date(start.year + month // 12, month % 12 + 1, 1)


def _periods(first: date, last: date, period: str) -> list[date]:
    """The starts of the periods from `first`'s to `last`'s, newest first."""
    out, start, floor = [], _period_start(last, period), _period_start(first, period)
    while start >= floor and len(out) < MAX_PERIODS[period]:
        out.append(start)
        start = _period_start(start - timedelta(days=1), period)
    return out


def _revenue(session_factory, settings: dict, now: datetime, *, public: bool = False) -> dict:
    """Revenue since settings.since. `public` (the public report): everything
    stops where settings.public_through says, the end of the last completed
    week by default. Either way the result says what of it is due for
    buyback: the revenue of completed periods (`due_cents`), less what
    customers paid of it in gamma."""
    since = _since(settings)
    excluded = settings["excluded_user_ids"]
    period, basis = settings["period"], settings["revenue_basis"]
    today = now.astimezone(UTC).date()
    current = _period_start(today, period)                 # the period in progress
    cut = datetime.combine(current, datetime.min.time(), tzinfo=UTC)
    if public:
        # `stop`: the first day NOT counted. `moves_on`: when it next moves.
        stop, moves_on = {
            "day": (today, today + timedelta(days=1)),
            "week": (_period_start(today, "week"), _period_start(today, "week") + timedelta(days=7)),
            "period": (current, _period_next(current, period)),
        }[settings["public_through"]]
        until = datetime.combine(stop, datetime.min.time(), tzinfo=UTC)
    else:
        # Staff: up to now. A payment staff dated ahead (tomorrow is
        # allowed, for time zones) counts from its day on.
        stop, moves_on, until = today + timedelta(days=1), None, None
    last_day = stop - timedelta(days=1)
    paid, gamma_by_user = paid_in(session_factory, since, until, excluded)
    for day, cents in contract_paid(session_factory, since.date(), stop).items():
        paid.setdefault(day, {})["contract"] = cents
    used_cents, used_hours, _ = compute_delivered(session_factory, since, until, excluded)

    def paid_day(d: date) -> int:
        return sum(paid.get(d, {}).values())

    def usage_day(d: date) -> int:
        # Contracts aren't metered: on either basis they count when paid.
        return used_cents.get(d, 0) + paid.get(d, {}).get("contract", 0)

    headline = paid_day if basis == "paid" else usage_day
    all_days = sorted(set(paid) | set(used_cents))
    by_method = {m: sum(_stream(v, m) for v in paid.values()) for m, _ in METHODS}
    by_currency = {c: sum(v.get(c, 0) for v in paid.values()) for c, _ in CURRENCIES}

    def gamma_covered(by_user: dict[str, int], to: datetime | None) -> int:
        """What of the revenue customers already bought back by paying in
        gamma. On the usage basis, the usage that gamma paid for: each
        account's gamma covers its own charges first, and nobody else's."""
        if basis == "paid":
            return sum(by_user.values())
        used = _used_by(session_factory, sorted(by_user), since, to)
        return sum(min(cents, used.get(user_id, 0)) for user_id, cents in by_user.items())

    total = sum(headline(d) for d in all_days)
    already = gamma_covered(gamma_by_user, until)
    due_days = [d for d in all_days if d < current]
    due = sum(headline(d) for d in due_days)
    if until is not None and stop <= current:
        already_due = already  # everything counted lies in completed periods
    elif basis == "paid":
        already_due = sum(paid.get(d, {}).get("gamma", 0) for d in due_days)
    else:
        already_due = gamma_covered(paid_in(session_factory, since, cut, excluded)[1], cut)

    rows = []
    if last_day >= since.date():
        for start in _periods(since.date(), last_day, period):
            end = _period_next(start, period) - timedelta(days=1)
            days = [d for d in all_days if start <= d <= end]
            usd_cents = sum(headline(d) for d in days)
            gamma_cents = sum(paid.get(d, {}).get("gamma", 0) for d in days)
            contract_cents = sum(paid.get(d, {}).get("contract", 0) for d in days)
            rows.append({
                "start": start.isoformat(), "end": end.isoformat(),
                # Set when the count starts part-way through the period.
                "counted_from": since.date().isoformat() if since.date() > start else None,
                "complete": end < current,
                "usd": _usd(usd_cents),
                **{f"{m}_usd": _usd(sum(_stream(paid.get(d, {}), m) for d in days)) for m, _ in METHODS},
                # For staff: the normal flow (everything but contracts) and
                # each currency of it; card and gamma are above.
                "normal_usd": _usd(usd_cents - contract_cents),
                **{f"{c}_usd": _usd(sum(paid.get(d, {}).get(c, 0) for d in days)) for c in CRYPTO},
                # What the owner buys back for the period: on the paid basis
                # everything but the gamma; not worked out per period on the
                # usage basis (gamma covers usage across periods).
                "to_buy_back_usd": _usd(max(0, usd_cents - gamma_cents)) if basis == "paid" else None,
                "compute_usd": _usd(sum(used_cents.get(d, 0) for d in days)),
                "gpu_hours": f"{sum(used_hours.get(d, 0.0) for d in days):.1f}",
            })
    last_days = [today - timedelta(days=i) for i in range(30) if today - timedelta(days=i) >= since.date()]
    return {
        "basis": basis,
        "basis_text": BASES[basis],
        "period": period,
        # The last day counted; None when no day is yet (the public report
        # before its first completed week, day or period).
        "through": last_day.isoformat() if last_day >= since.date() else None,
        # The public report: how fresh it is, and the day it next moves on.
        "public_through": settings["public_through"] if public else None,
        "next_figures_on": moves_on.isoformat() if moves_on else None,
        "total_cents": total,
        "total_usd": _usd(total),
        "paid_in_usd": _usd(sum(by_method.values())),
        "by_method": [{"method": m, "label": label, "usd": _usd(by_method[m])} for m, label in METHODS],
        "by_method_cents": by_method,
        # For staff: the two flows, and the normal flow by currency.
        "normal_usd": _usd(total - by_method["contract"]),
        "contracts_usd": _usd(by_method["contract"]),
        "by_currency": [{"currency": c, "label": label, "usd": _usd(by_currency[c])} for c, label in CURRENCIES],
        "bought_back_by_customers_cents": min(already, max(0, total)),
        "due_cents": due,
        "due_by_customers_cents": min(already_due, max(0, due)),
        "next_due_day": _period_next(current, period).isoformat(),
        "periods": rows,
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
    # ours to buy back, and falls due when its period has ended.
    owed = max(0, revenue["total_cents"] - by_customers)
    due = min(owed, max(0, revenue["due_cents"] - revenue["due_by_customers_cents"]))
    return {
        "period": revenue["period"],
        "to_buy_back_cents": due,
        "to_buy_back_usd": _usd(due),
        "bought_back_usd": _usd(bought_cents),
        "outstanding_cents": max(0, due - bought_cents),
        "outstanding_usd": _usd(max(0, due - bought_cents)),
        "paid_in_gamma_usd": _usd(by_customers),
        # The period in progress: not due until it ends (`next_due`).
        "accruing_usd": _usd(owed - due),
        "next_due": revenue["next_due_day"],
        # Everything up to today, due or not, less what was bought back.
        "settle_now_cents": max(0, owed - bought_cents),
        "settle_now_usd": _usd(max(0, owed - bought_cents)),
        "log": log_rows[:50],
        "on_chain": {
            "netuid": netuid,
            "service_hotkey": g.service_hotkey(),
            "service_hotkeys": g.service_hotkeys(),
            "spent_gamma": g.gamma_text(spent_raw),
            "scope": "All gamma ever spent at our service hotkeys: customers' payments (credited, or waiting for "
                     "the weekly gamma limit), our buybacks and any spends we refused to credit. It counts from "
                     "the first spend, not from the report's start.",
            "as_of_block": last_block(session_factory),
            "how_to_check": "SubtensorModule.GammaSpent(netuid, service hotkey, *) at any finalized block: "
                            "the sum is every gamma payment and buyback made here.",
        },
    }


def buyback(session_factory, *, now: datetime | None = None) -> dict:
    """What is due, what is building up in the period in progress, and what
    one buyback takes: for staff."""
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

    def in_tao(cents: int) -> float | None:
        return round(cents / 100 / tao_usd, 4) if tao_usd and cents > 0 else None

    tao = in_tao(out["outstanding_cents"])
    settle = in_tao(out["settle_now_cents"])
    netuid, service = g.netuid(), g.service_hotkey()
    return {
        "since": settings["since"],
        "basis": settings["revenue_basis"],
        "revenue_usd": revenue["total_usd"],
        **{k: v for k, v in out.items() if not k.endswith("_cents")},
        "tao_usd": tao_usd,
        "tao_to_mint": f"{tao:.4f}" if tao else None,
        "settle_now_tao": f"{settle:.4f}" if settle else None,
        "commands": [
            f"btcli gamma mint --netuid {netuid} --hotkey gc-buyback --amount-tao {tao:.4f}",
            f"btcli gamma spend --netuid {netuid} --service-hotkey {service} --amount-gamma <the gamma the mint gave> "
            "-H gc-buyback",
        ] if tao else [],
        "periods": [{"start": p["start"], "end": p["end"], "counted_from": p["counted_from"],
                     "complete": p["complete"], "revenue_usd": p["usd"], "gamma_usd": p["gamma_usd"],
                     "to_buy_back_usd": p["to_buy_back_usd"]} for p in revenue["periods"]],
        "note": "A buyback falls due when its period has ended. Signed by the subnet owner coldkey: mint gamma "
                "with the TAO into the buyback hotkey, then spend it at our service hotkey. The gateway records "
                "the spend as a buyback within a minute; it is never credited to an account. Keep the deposit "
                "open until it shows here.",
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


def _farmer_payments(session_factory, since: datetime, before: date | None = None) -> dict:
    """What farmers were paid, by statement month. `before` (the public
    report): only months that ended before that day, the start of the
    period in progress, as for revenue."""
    with session_scope(session_factory) as session:
        query = (
            select(FarmerPaymentORM.month, func.sum(FarmerPaymentORM.amount_cents))
            .where(FarmerPaymentORM.month >= since.strftime("%Y-%m"))
            .group_by(FarmerPaymentORM.month).order_by(FarmerPaymentORM.month.desc())
        )
        if before is not None:
            query = query.where(FarmerPaymentORM.month < before.strftime("%Y-%m"))
        rows = session.execute(query).all()
    return {"total_usd": _usd(sum(int(c or 0) for _, c in rows)),
            "months": [{"month": m, "usd": _usd(int(c or 0))} for m, c in rows]}


# --- the report ---------------------------------------------------------------------


def build(session_factory, *, admin: bool = False, now: datetime | None = None) -> dict:
    """The report. `admin`: for staff, with exact GPU counts, farmers' names
    and ids, the period in progress and single days; otherwise what the
    public gets."""
    now = now or _now()
    settings = get_settings(session_factory)
    farmers = {f["farmer_id"]: f for f in list_farmers(session_factory)}
    all_machines = machines(session_factory, settings=settings, now=now)
    revenue = _revenue(session_factory, settings, now, public=not admin)
    buybacks = _buybacks(session_factory, settings, revenue)
    summary = _gpu_summary([m for m in all_machines if m["listed"]],
                           contract_capacity(session_factory, now.astimezone(UTC).date()), farmers, admin=admin)
    if admin or settings["gpu_detail"] == "exact":
        gpus = {"detail": "exact", **summary}
        gpus["by_farmer"] = [{k: v for k, v in f.items() if not k.startswith("_")} for f in summary["by_farmer"]]
        if not admin and not settings["show_providers"]:
            gpus["by_farmer"] = []
    else:
        gpus = _gpu_bands(summary, show_providers=settings["show_providers"])
    gpus["listed_within_days"] = settings["listed_within_days"]

    def public(d: dict) -> dict:
        return {k: v for k, v in d.items() if not k.endswith("_cents") and k != "next_due_day"}

    out = {
        "generated_at": now.isoformat(timespec="seconds"),
        "netuid": g.netuid(),
        "published": bool(settings["published"]),
        "since": settings["since"],
        "period": settings["period"],
        "gpus": gpus,
        "revenue": public(revenue),
        "buybacks": public(buybacks),
    }
    if admin:
        out["revenue"].pop("public_through", None)
    else:
        # Single days are staff's; so is what a buyback today would take.
        out["revenue"].pop("days", None)
        out["buybacks"].pop("settle_now_usd", None)
        if settings["show_contracts"]:
            through = revenue["through"]
            out["contracts"] = public_contracts(
                session_factory, settings, through=date.fromisoformat(through) if through else None,
                today=now.astimezone(UTC).date())
        if not settings["show_gpu_hours"]:
            out["revenue"].pop("compute_delivered", None)
            for row in out["revenue"]["periods"]:
                row.pop("compute_usd", None)
                row.pop("gpu_hours", None)
    if admin:
        out["farmer_payments"] = _farmer_payments(session_factory, _since(settings))
    elif settings["show_farmer_payments"]:
        # Only statement months that ended before the first day not yet
        # public, as for revenue.
        through = revenue["through"]
        not_yet = (date.fromisoformat(through) + timedelta(days=1)) if through else _since(settings).date()
        out["farmer_payments"] = _farmer_payments(session_factory, _since(settings), before=not_yet)
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

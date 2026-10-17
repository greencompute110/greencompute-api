"""Admin commercial analytics — aggregations over ledger, miner payouts, and
deployment state for the operator dashboard.

Kept separate from BillingRepository so the ledger-write path doesn't take a
dependency on read-heavy aggregations (and so we can move these to a read
replica later without rewiring billing).

Every method here is read-only and admin-gated at the route layer. They take
a `days` window so the same dashboard can render 24h / 7d / 30d without
multiple endpoints.
"""
from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select

from greencompute_persistence import (
    create_db_engine,
    create_session_factory,
    init_database,
    session_scope,
)
from greencompute_persistence.db import needs_bootstrap
from greencompute_persistence.orm import (
    CryptoInvoiceORM,
    DeploymentORM,
    LedgerEntryORM,
    MinerPayoutAccrualORM,
    NodeInventoryORM,
    StripeSessionORM,
    UserORM,
    WorkloadORM,
)


# GPU classes we sell, always listed in gpu_usage even with nothing online.
_OFFERED_GPUS = ("rtx4090", "rtx5090")
# Deployment states that hold GPUs on a node.
_HOLDING_STATES = ("scheduled", "pulling", "starting", "ready", "draining")
# How a request turned away for lack of capacity ends (scheduler / node agent).
_NO_CAPACITY_ERRORS = ("no compatible miner capacity%", "GPU allocation failed%")


def _gpu_key(model: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (model or "").lower()) or "unknown"


def _gpu_label(key: str) -> str:
    m = re.fullmatch(r"(rtx|gtx|a|h|l)(\d+)(.*)", key)
    if key == "any":
        return "Any GPU"
    return f"{m.group(1).upper()} {m.group(2)}{m.group(3).upper()}".strip() if m else key.upper()


def _aware(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=UTC)


# Cap how far back any single analytics call can scan — keeps slow SELECTs
# from someone curling `?days=99999` and blowing through the index.
_MAX_DAYS = 365

# Ledger `kind` values that represent user SPEND (stored as negative debits).
# Both 'usage' (GPU rental metering) and 'inference' (per-token) are real
# revenue; the analytics aggregations must count both. Refunds/adjustments are
# NOT in this set so claw-backs don't inflate "spend".
_DEBIT_KINDS = ("usage", "inference")


def _cutoff(days: int) -> datetime:
    days = max(1, min(int(days or 7), _MAX_DAYS))
    return datetime.now(UTC) - timedelta(days=days)


class AnalyticsRepository:
    def __init__(
        self,
        database_url: str | None = None,
        bootstrap: bool | None = None,
    ) -> None:
        self.engine = create_db_engine(database_url)
        self.session_factory = create_session_factory(self.engine)
        if needs_bootstrap(str(self.engine.url), bootstrap):
            init_database(self.engine)

    # --- Revenue per miner --------------------------------------------------

    def revenue_by_miner(self, *, days: int, limit: int = 100) -> list[dict]:
        """Sum miner_payout_accrual.cents_earned per hotkey in window."""
        cutoff = _cutoff(days)
        with session_scope(self.session_factory) as session:
            rows = session.execute(
                select(
                    MinerPayoutAccrualORM.hotkey,
                    func.count(MinerPayoutAccrualORM.accrual_id).label("requests"),
                    func.coalesce(
                        func.sum(MinerPayoutAccrualORM.cents_earned), 0
                    ).label("cents_earned"),
                    func.coalesce(
                        func.sum(MinerPayoutAccrualORM.prompt_tokens), 0
                    ).label("prompt_tokens"),
                    func.coalesce(
                        func.sum(MinerPayoutAccrualORM.completion_tokens), 0
                    ).label("completion_tokens"),
                )
                .where(MinerPayoutAccrualORM.created_at >= cutoff)
                .group_by(MinerPayoutAccrualORM.hotkey)
                .order_by(
                    func.sum(MinerPayoutAccrualORM.cents_earned).desc()
                )
                .limit(limit)
            ).all()
            return [
                {
                    "hotkey": r.hotkey,
                    "requests": int(r.requests),
                    "cents_earned": int(r.cents_earned),
                    "prompt_tokens": int(r.prompt_tokens),
                    "completion_tokens": int(r.completion_tokens),
                }
                for r in rows
            ]

    # --- Top renters (by spend) --------------------------------------------

    def top_renters(self, *, days: int, limit: int = 25) -> list[dict]:
        """Largest spenders by usage-debit volume in window.

        Ledger entries with kind='usage' are negative (debit). We sum the
        absolute value so the dashboard reads naturally as "$ spent".
        Joins UserORM only for display fields — the username/email are
        nullable in dev fixtures, so we coalesce.
        """
        cutoff = _cutoff(days)
        spend = func.coalesce(func.sum(-LedgerEntryORM.amount_cents), 0).label(
            "cents_spent"
        )
        with session_scope(self.session_factory) as session:
            stmt = (
                select(
                    LedgerEntryORM.user_id,
                    UserORM.username,
                    UserORM.email,
                    func.count(LedgerEntryORM.entry_id).label("debit_count"),
                    spend,
                )
                .join(UserORM, UserORM.user_id == LedgerEntryORM.user_id, isouter=True)
                .where(
                    LedgerEntryORM.kind.in_(_DEBIT_KINDS),
                    LedgerEntryORM.amount_cents < 0,
                    LedgerEntryORM.created_at >= cutoff,
                )
                .group_by(LedgerEntryORM.user_id, UserORM.username, UserORM.email)
                .order_by(spend.desc())
                .limit(limit)
            )
            rows = session.execute(stmt).all()
            return [
                {
                    "user_id": r.user_id,
                    "username": r.username or "",
                    "email": r.email or "",
                    "debit_count": int(r.debit_count),
                    "cents_spent": int(r.cents_spent),
                }
                for r in rows
            ]

    # --- Gross revenue ------------------------------------------------------

    def gross_revenue(self, *, days: int) -> dict:
        """Period totals — money in (top-ups) vs. money flowing through usage.

        Top-ups are recognized at the moment the payment confirms (not when
        the session/invoice was created). Usage is the same window's debit
        sum.
        """
        cutoff = _cutoff(days)
        with session_scope(self.session_factory) as session:
            stripe_total_cents = int(
                session.execute(
                    select(
                        func.coalesce(func.sum(StripeSessionORM.amount_cents), 0)
                    ).where(
                        StripeSessionORM.status == "paid",
                        StripeSessionORM.completed_at != None,  # noqa: E711
                        StripeSessionORM.completed_at >= cutoff,
                    )
                ).scalar_one()
            )
            # Crypto invoices store dollars (amount_usd) — convert to cents
            # for parity with the rest of the dashboard. total_credits is the
            # bonus-inclusive credit grant; we report both so finance can tell
            # apart "money received" from "credit issued."
            crypto_row = session.execute(
                select(
                    func.coalesce(func.sum(CryptoInvoiceORM.amount_usd), 0.0).label(
                        "amount_usd"
                    ),
                    func.coalesce(
                        func.sum(CryptoInvoiceORM.total_credits), 0
                    ).label("credits"),
                ).where(
                    CryptoInvoiceORM.status == "confirmed",
                    CryptoInvoiceORM.confirmed_at != None,  # noqa: E711
                    CryptoInvoiceORM.confirmed_at >= cutoff,
                )
            ).one()
            crypto_received_cents = int(round(float(crypto_row.amount_usd) * 100))
            crypto_credits_granted = int(crypto_row.credits)

            usage_debit_cents = int(
                session.execute(
                    select(
                        func.coalesce(func.sum(-LedgerEntryORM.amount_cents), 0)
                    ).where(
                        LedgerEntryORM.kind.in_(_DEBIT_KINDS),
                        LedgerEntryORM.amount_cents < 0,
                        LedgerEntryORM.created_at >= cutoff,
                    )
                ).scalar_one()
            )
        return {
            "stripe_received_cents": stripe_total_cents,
            "crypto_received_cents": crypto_received_cents,
            "crypto_credits_granted_cents": crypto_credits_granted,
            "topups_received_cents": stripe_total_cents + crypto_received_cents,
            "usage_debited_cents": usage_debit_cents,
            "window_days": max(1, min(int(days or 7), _MAX_DAYS)),
        }

    # --- Active rentals / fleet utilisation ---------------------------------

    def gpu_usage(self, *, days: int = 7, now: datetime | None = None) -> dict:
        """Per GPU class: GPUs online (nodes reporting) and offline (registered
        but silent), in use by rentals or by models, free, and requests turned
        away for lack of capacity in the window — the signal for asking miners
        to add GPUs."""
        days = max(1, min(days, _MAX_DAYS))
        now = now or datetime.now(UTC)
        stale_after = timedelta(seconds=int(os.environ.get("GREENCOMPUTE_NODE_INVENTORY_TIMEOUT_SECONDS") or 900))
        classes: dict[str, dict] = {}

        def bucket(key: str) -> dict:
            return classes.setdefault(key, {
                "gpu_model": key, "label": _gpu_label(key),
                "online_gpus": 0, "offline_gpus": 0, "nodes_online": 0, "nodes_offline": 0,
                "rented_gpus": 0, "model_gpus": 0,
                "turned_away_requests": 0, "turned_away_gpus": 0, "turned_away_customers": 0,
            })

        for key in _OFFERED_GPUS:
            bucket(key)
        with session_scope(self.session_factory) as session:
            node_class: dict[str, str] = {}
            for node_id, payload, observed_at in session.execute(
                select(NodeInventoryORM.node_id, NodeInventoryORM.payload, NodeInventoryORM.observed_at)
            ).all():
                payload = payload if isinstance(payload, dict) else {}
                key = _gpu_key(payload.get("gpu_model"))
                node_class[node_id] = key
                b = bucket(key)
                count = int(payload.get("gpu_count") or 0)
                seen = _aware(observed_at)
                if seen is not None and now - seen <= stale_after:
                    b["online_gpus"] += count
                    b["nodes_online"] += 1
                else:
                    b["offline_gpus"] += count
                    b["nodes_offline"] += 1

            for node_id, kind, requirements, ready_instances in session.execute(
                select(DeploymentORM.node_id, WorkloadORM.kind, WorkloadORM.requirements, DeploymentORM.ready_instances)
                .join(WorkloadORM, WorkloadORM.workload_id == DeploymentORM.workload_id, isouter=True)
                .where(DeploymentORM.state.in_(_HOLDING_STATES), DeploymentORM.node_id.is_not(None))
            ).all():
                key = node_class.get(node_id)
                if key is None:
                    continue
                per_instance = (requirements or {}).get("gpu_count") if isinstance(requirements, dict) else None
                gpus = (per_instance if isinstance(per_instance, int) and per_instance > 0 else 1) * max(ready_instances or 0, 1)
                classes[key]["model_gpus" if kind == "inference" else "rented_gpus"] += gpus

            customers: dict[str, set] = {}
            for owner, requirements in session.execute(
                select(DeploymentORM.owner_user_id, WorkloadORM.requirements)
                .join(WorkloadORM, WorkloadORM.workload_id == DeploymentORM.workload_id, isouter=True)
                .where(
                    DeploymentORM.state == "failed",
                    DeploymentORM.updated_at >= now - timedelta(days=days),
                    DeploymentORM.owner_user_id.is_not(None),
                    or_(*(DeploymentORM.last_error.like(p) for p in _NO_CAPACITY_ERRORS)),
                )
            ).all():
                req = requirements if isinstance(requirements, dict) else {}
                wanted = {_gpu_key(m) for m in (req.get("supported_gpu_models") or [])}
                key = wanted.pop() if len(wanted) == 1 else "any"
                b = bucket(key)
                per_instance = req.get("gpu_count")
                b["turned_away_requests"] += 1
                b["turned_away_gpus"] += per_instance if isinstance(per_instance, int) and per_instance > 0 else 1
                customers.setdefault(key, set()).add(owner)
            for key, owners in customers.items():
                classes[key]["turned_away_customers"] = len(owners)

        for b in classes.values():
            in_use = b["rented_gpus"] + b["model_gpus"]
            b["in_use_gpus"] = in_use
            b["free_gpus"] = max(b["online_gpus"] - in_use, 0)
            b["utilization_pct"] = round(100 * in_use / b["online_gpus"], 1) if b["online_gpus"] else None
        order = {key: i for i, key in enumerate(_OFFERED_GPUS)}
        rows = sorted(classes.values(), key=lambda b: (order.get(b["gpu_model"], 99), b["gpu_model"] == "any", b["gpu_model"]))
        return {"generated_at": now.isoformat(), "days": days, "classes": rows}

    def active_rentals(self) -> dict:
        """Snapshot of READY deployments + their GPU and dollar footprint.

        GPU count comes from the joined workload's requirements JSON
        (`requirements.gpu_count`). Hourly $ is summed over deployments using
        the placement-locked `hourly_rate_cents` * the workload's per-instance
        gpu_count * ready_instances (falling back to requested_instances when
        the deployment hasn't been marked READY end-to-end yet).
        """
        with session_scope(self.session_factory) as session:
            rows = session.execute(
                select(
                    DeploymentORM.deployment_id,
                    DeploymentORM.hotkey,
                    DeploymentORM.requested_instances,
                    DeploymentORM.ready_instances,
                    DeploymentORM.hourly_rate_cents,
                    DeploymentORM.created_at,
                    WorkloadORM.requirements,
                )
                .join(WorkloadORM, WorkloadORM.workload_id == DeploymentORM.workload_id, isouter=True)
                .where(DeploymentORM.state == "ready")
            ).all()

            count = 0
            total_gpus = 0
            total_hourly_cents = 0
            gpu_buckets: dict[int, int] = {}  # gpu_count → deployments-with-that-size
            for r in rows:
                gpu_per_instance = 1
                if isinstance(r.requirements, dict):
                    raw = r.requirements.get("gpu_count")
                    if isinstance(raw, int) and raw > 0:
                        gpu_per_instance = raw
                instances = max(r.ready_instances or 0, r.requested_instances or 1)
                gpus = gpu_per_instance * instances
                count += 1
                total_gpus += gpus
                total_hourly_cents += int(r.hourly_rate_cents or 0) * gpus
                gpu_buckets[gpus] = gpu_buckets.get(gpus, 0) + 1

            return {
                "active_count": count,
                "total_gpus": total_gpus,
                "avg_gpus_per_deployment": (total_gpus / count) if count else 0.0,
                "hourly_cents_running": total_hourly_cents,
                "size_distribution": [
                    {"gpus": g, "deployments": n}
                    for g, n in sorted(gpu_buckets.items())
                ],
            }

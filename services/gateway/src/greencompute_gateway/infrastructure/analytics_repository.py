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

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

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
    StripeSessionORM,
    UserORM,
    WorkloadORM,
)


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

"""Per-API-key spending, for the key's optional limits.

A key's spend is every charge on rentals it started (GPU time and saved-pod
storage, matched through deployments.created_by_key_id) plus every inference
request it made (matched through invocation_records.api_key_id). Shared by the
gateway (checks before starting a rental or serving a request) and the control
plane (stops running rentals once a key reaches its limit).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from greencompute_persistence.db import session_scope
from greencompute_persistence.orm import APIKeyORM, DeploymentORM, InvocationRecordORM, LedgerEntryORM

SPEND_WINDOW = timedelta(hours=24)

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

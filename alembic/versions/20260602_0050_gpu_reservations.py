"""gpu_reservations — authoritative GPU-reservation ledger (ORCH-C2)

Adds a NEW append-style table `gpu_reservations` that is the scheduler's own
record of which GPUs a deployment holds on a (hotkey, node). It is a SEPARATE
source of truth from the miner-reported `capacity.available_gpus`, so a stale
or clobbering heartbeat can never silently free GPUs the control-plane has
committed. Effective availability is later DERIVED on read as the stricter of
(physically-reported, total − sum(active reservations)).

This migration is strictly additive:
  - CREATE TABLE `gpu_reservations` + a composite index. No existing table is
    altered, renamed, or dropped; `capacity.available_gpus` is left intact and
    untouched (it merely stops being the SOLE truth).
  - BACKFILL one `status='active'` reservation per currently-active
    `lease_assignments` row (status in assigned/activating/active) that has a
    non-empty `node_id`, sized from the workload's `requirements.gpu_count`.
    This immediately represents the live tenant leases (e.g. ae82919d /
    a6086366) so they are NOT double-spent the moment derived availability
    goes live.

COLLISION-SAFETY (must never abort on prod):
  - The table is brand-new and EMPTY before this runs, so there is nothing to
    conflict with.
  - `lease_assignments.deployment_id` is itself UNIQUE (orm.py), so selecting
    one reservation per active lease yields at most one row per
    `deployment_id` — the table's `UNIQUE(deployment_id)` cannot be violated.
  - As defence-in-depth we still DISTINCT on `deployment_id` and pick the
    lexicographically-smallest `assignment_id` per deployment, and guard with a
    `NOT EXISTS` against any row already carrying that `deployment_id`. So the
    backfill is incapable of raising an IntegrityError regardless of data.
  - Touches NO balances and NO existing column.

Reversible: downgrade() drops only the new table (no FK, so no orphans).

Revision ID: 20260602_0050
Revises: 20260602_0049
Create Date: 2026-06-02
"""
from alembic import op
import sqlalchemy as sa


revision = "20260602_0050"
down_revision = "20260602_0049"
branch_labels = None
depends_on = None


# Active lease statuses mirror the control-plane's own definition
# (services.list_leases -> ["assigned", "activating", "active"]).
_ACTIVE_LEASE_STATUSES = ("assigned", "activating", "active")


# Postgres: `requirements` is a json column, so `->>` extracts the text value
# of gpu_count; COALESCE/NULLIF guards a missing/empty key to 0. We synthesise a
# deterministic reservation_id from the deployment_id ("resv:" || deployment_id)
# so the backfill is itself idempotent (re-running cannot create a 2nd row).
_BACKFILL_POSTGRES = """
INSERT INTO gpu_reservations
    (reservation_id, deployment_id, hotkey, node_id, gpu_count, status, created_at, released_at)
SELECT
    'resv:' || la.deployment_id,
    la.deployment_id,
    la.hotkey,
    la.node_id,
    COALESCE(NULLIF((w.requirements ->> 'gpu_count'), '')::integer, 0),
    'active',
    now(),
    NULL
FROM lease_assignments AS la
JOIN workloads AS w ON w.workload_id = la.workload_id
WHERE la.status IN ('assigned', 'activating', 'active')
  AND la.node_id IS NOT NULL
  AND la.node_id <> ''
  AND la.assignment_id = (
        SELECT MIN(la2.assignment_id) FROM lease_assignments la2
        WHERE la2.deployment_id = la.deployment_id
          AND la2.status IN ('assigned', 'activating', 'active')
          AND la2.node_id IS NOT NULL
          AND la2.node_id <> ''
  )
  AND NOT EXISTS (
        SELECT 1 FROM gpu_reservations gr
        WHERE gr.deployment_id = la.deployment_id
  )
"""

# SQLite (used in tests): json_extract reads gpu_count; missing -> NULL ->
# coalesced to 0. Same one-row-per-deployment + NOT EXISTS guards.
_BACKFILL_SQLITE = """
INSERT INTO gpu_reservations
    (reservation_id, deployment_id, hotkey, node_id, gpu_count, status, created_at, released_at)
SELECT
    'resv:' || la.deployment_id,
    la.deployment_id,
    la.hotkey,
    la.node_id,
    COALESCE(CAST(json_extract(w.requirements, '$.gpu_count') AS INTEGER), 0),
    'active',
    CURRENT_TIMESTAMP,
    NULL
FROM lease_assignments AS la
JOIN workloads AS w ON w.workload_id = la.workload_id
WHERE la.status IN ('assigned', 'activating', 'active')
  AND la.node_id IS NOT NULL
  AND la.node_id <> ''
  AND la.assignment_id = (
        SELECT MIN(la2.assignment_id) FROM lease_assignments la2
        WHERE la2.deployment_id = la.deployment_id
          AND la2.status IN ('assigned', 'activating', 'active')
          AND la2.node_id IS NOT NULL
          AND la2.node_id <> ''
  )
  AND NOT EXISTS (
        SELECT 1 FROM gpu_reservations gr
        WHERE gr.deployment_id = la.deployment_id
  )
"""


def upgrade() -> None:
    op.create_table(
        "gpu_reservations",
        sa.Column("reservation_id", sa.String(64), primary_key=True),
        sa.Column("deployment_id", sa.String(64), nullable=False),
        sa.Column("hotkey", sa.String(128), nullable=False),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("gpu_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("deployment_id", name="uq_gpu_reservations_deployment"),
    )
    op.create_index("ix_gpu_reservations_deployment_id", "gpu_reservations", ["deployment_id"])
    op.create_index("ix_gpu_reservations_hotkey", "gpu_reservations", ["hotkey"])
    op.create_index("ix_gpu_reservations_node_id", "gpu_reservations", ["node_id"])
    op.create_index("ix_gpu_reservations_status", "gpu_reservations", ["status"])
    op.create_index(
        "ix_gpu_reservations_hotkey_node_status",
        "gpu_reservations",
        ["hotkey", "node_id", "status"],
    )

    # Backfill one active reservation per currently-active lease so live leases
    # are represented (and therefore not double-spent) the instant derived
    # availability goes live.
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute(_BACKFILL_SQLITE)
    else:
        op.execute(_BACKFILL_POSTGRES)


def downgrade() -> None:
    # New table only — dropping it leaves every pre-existing table (leases,
    # capacity, deployments, …) untouched and creates no FK orphans.
    op.drop_index("ix_gpu_reservations_hotkey_node_status", table_name="gpu_reservations")
    op.drop_index("ix_gpu_reservations_status", table_name="gpu_reservations")
    op.drop_index("ix_gpu_reservations_node_id", table_name="gpu_reservations")
    op.drop_index("ix_gpu_reservations_hotkey", table_name="gpu_reservations")
    op.drop_index("ix_gpu_reservations_deployment_id", table_name="gpu_reservations")
    op.drop_table("gpu_reservations")

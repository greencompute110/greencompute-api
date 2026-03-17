"""add ops hardening state

Revision ID: 20260317_0012
Revises: 20260317_0011
Create Date: 2026-03-17 00:12:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_0012"
down_revision = "20260317_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("builds", sa.Column("failure_class", sa.String(length=128), nullable=True))
    op.add_column("builds", sa.Column("last_operation", sa.String(length=128), nullable=True))
    op.add_column("builds", sa.Column("cleanup_status", sa.String(length=128), nullable=True))
    op.add_column("builds", sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"))

    op.add_column("deployments", sa.Column("failure_class", sa.String(length=128), nullable=True))
    op.add_column("deployments", sa.Column("last_retry_reason", sa.Text(), nullable=True))
    op.add_column("deployments", sa.Column("retry_exhausted", sa.Boolean(), nullable=False, server_default=sa.false()))

    op.create_table(
        "lease_history",
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("deployment_id", sa.String(length=64), nullable=False),
        sa.Column("workload_id", sa.String(length=64), nullable=False),
        sa.Column("hotkey", sa.String(length=128), nullable=False),
        sa.Column("node_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index("ix_lease_history_deployment_id", "lease_history", ["deployment_id"])
    op.create_index("ix_lease_history_workload_id", "lease_history", ["workload_id"])
    op.create_index("ix_lease_history_hotkey", "lease_history", ["hotkey"])
    op.create_index("ix_lease_history_node_id", "lease_history", ["node_id"])
    op.create_index("ix_lease_history_status", "lease_history", ["status"])
    op.create_index("ix_lease_history_observed_at", "lease_history", ["observed_at"])


def downgrade() -> None:
    op.drop_index("ix_lease_history_observed_at", table_name="lease_history")
    op.drop_index("ix_lease_history_status", table_name="lease_history")
    op.drop_index("ix_lease_history_node_id", table_name="lease_history")
    op.drop_index("ix_lease_history_hotkey", table_name="lease_history")
    op.drop_index("ix_lease_history_workload_id", table_name="lease_history")
    op.drop_index("ix_lease_history_deployment_id", table_name="lease_history")
    op.drop_table("lease_history")

    op.drop_column("deployments", "retry_exhausted")
    op.drop_column("deployments", "last_retry_reason")
    op.drop_column("deployments", "failure_class")

    op.drop_column("builds", "retry_count")
    op.drop_column("builds", "cleanup_status")
    op.drop_column("builds", "last_operation")
    op.drop_column("builds", "failure_class")

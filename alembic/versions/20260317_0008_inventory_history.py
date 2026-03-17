"""add inventory and placement history

Revision ID: 20260317_0008
Revises: 20260317_0007
Create Date: 2026-03-17 00:08:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_0008"
down_revision = "20260317_0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "servers",
        sa.Column("server_id", sa.String(length=128), nullable=False),
        sa.Column("hotkey", sa.String(length=128), nullable=False),
        sa.Column("hostname", sa.String(length=255), nullable=True),
        sa.Column("api_base_url", sa.String(length=512), nullable=True),
        sa.Column("validator_url", sa.String(length=512), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("server_id"),
    )
    op.create_index("ix_servers_hotkey", "servers", ["hotkey"])

    op.create_table(
        "node_inventory",
        sa.Column("node_id", sa.String(length=128), nullable=False),
        sa.Column("hotkey", sa.String(length=128), nullable=False),
        sa.Column("server_id", sa.String(length=128), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("node_id"),
    )
    op.create_index("ix_node_inventory_hotkey", "node_inventory", ["hotkey"])
    op.create_index("ix_node_inventory_server_id", "node_inventory", ["server_id"])

    op.create_table(
        "capacity_history",
        sa.Column("history_id", sa.String(length=64), nullable=False),
        sa.Column("hotkey", sa.String(length=128), nullable=False),
        sa.Column("server_id", sa.String(length=128), nullable=True),
        sa.Column("node_id", sa.String(length=128), nullable=False),
        sa.Column("available_gpus", sa.Integer(), nullable=False),
        sa.Column("total_gpus", sa.Integer(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("history_id"),
    )
    op.create_index("ix_capacity_history_hotkey", "capacity_history", ["hotkey"])
    op.create_index("ix_capacity_history_server_id", "capacity_history", ["server_id"])
    op.create_index("ix_capacity_history_node_id", "capacity_history", ["node_id"])
    op.create_index("ix_capacity_history_observed_at", "capacity_history", ["observed_at"])

    op.create_table(
        "placements",
        sa.Column("placement_id", sa.String(length=64), nullable=False),
        sa.Column("deployment_id", sa.String(length=64), nullable=False),
        sa.Column("workload_id", sa.String(length=64), nullable=False),
        sa.Column("hotkey", sa.String(length=128), nullable=False),
        sa.Column("server_id", sa.String(length=128), nullable=True),
        sa.Column("node_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("placement_id"),
    )
    op.create_index("ix_placements_deployment_id", "placements", ["deployment_id"])
    op.create_index("ix_placements_workload_id", "placements", ["workload_id"])
    op.create_index("ix_placements_hotkey", "placements", ["hotkey"])
    op.create_index("ix_placements_server_id", "placements", ["server_id"])
    op.create_index("ix_placements_node_id", "placements", ["node_id"])
    op.create_index("ix_placements_status", "placements", ["status"])


def downgrade() -> None:
    op.drop_index("ix_placements_status", table_name="placements")
    op.drop_index("ix_placements_node_id", table_name="placements")
    op.drop_index("ix_placements_server_id", table_name="placements")
    op.drop_index("ix_placements_hotkey", table_name="placements")
    op.drop_index("ix_placements_workload_id", table_name="placements")
    op.drop_index("ix_placements_deployment_id", table_name="placements")
    op.drop_table("placements")

    op.drop_index("ix_capacity_history_observed_at", table_name="capacity_history")
    op.drop_index("ix_capacity_history_node_id", table_name="capacity_history")
    op.drop_index("ix_capacity_history_server_id", table_name="capacity_history")
    op.drop_index("ix_capacity_history_hotkey", table_name="capacity_history")
    op.drop_table("capacity_history")

    op.drop_index("ix_node_inventory_server_id", table_name="node_inventory")
    op.drop_index("ix_node_inventory_hotkey", table_name="node_inventory")
    op.drop_table("node_inventory")

    op.drop_index("ix_servers_hotkey", table_name="servers")
    op.drop_table("servers")

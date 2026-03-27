"""add flux orchestrator tables

Revision ID: 20260326_0026
Revises: 20260318_0025
Create Date: 2026-03-26 00:26:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260326_0026"
down_revision = "20260318_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "flux_states",
        sa.Column("hotkey", sa.String(128), primary_key=True),
        sa.Column("node_id", sa.String(128), nullable=False, index=True),
        sa.Column("total_gpus", sa.Integer(), nullable=False),
        sa.Column("inference_gpus", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rental_gpus", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("idle_gpus", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("inference_floor_pct", sa.Float(), nullable=False, server_default="0.20"),
        sa.Column("rental_floor_pct", sa.Float(), nullable=False, server_default="0.10"),
        sa.Column("inference_demand_score", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("rental_demand_score", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("last_rebalanced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "flux_rebalance_events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("hotkey", sa.String(128), nullable=False, index=True),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("gpu_index", sa.Integer(), nullable=False),
        sa.Column("from_mode", sa.String(32), nullable=False),
        sa.Column("to_mode", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "rental_wait_queue",
        sa.Column("deployment_id", sa.String(128), primary_key=True),
        sa.Column("hotkey", sa.String(128), nullable=False, index=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("estimated_wait_seconds", sa.Float(), nullable=False, server_default="0.0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("rental_wait_queue")
    op.drop_table("flux_rebalance_events")
    op.drop_table("flux_states")

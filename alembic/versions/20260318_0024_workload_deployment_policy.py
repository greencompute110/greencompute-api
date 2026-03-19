"""add workload lifecycle and deployment policy fields

Revision ID: 20260318_0024
Revises: 20260318_0023
Create Date: 2026-03-18 03:40:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260318_0024"
down_revision = "20260318_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workloads",
        sa.Column("lifecycle", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.alter_column("workloads", "lifecycle", server_default=None)

    op.add_column(
        "deployments",
        sa.Column("deployment_fee_usd", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column(
        "deployments",
        sa.Column("fee_acknowledged", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "deployments",
        sa.Column("warmup_state", sa.String(length=32), nullable=False, server_default="pending"),
    )
    op.alter_column("deployments", "deployment_fee_usd", server_default=None)
    op.alter_column("deployments", "fee_acknowledged", server_default=None)
    op.alter_column("deployments", "warmup_state", server_default=None)


def downgrade() -> None:
    op.drop_column("deployments", "warmup_state")
    op.drop_column("deployments", "fee_acknowledged")
    op.drop_column("deployments", "deployment_fee_usd")
    op.drop_column("workloads", "lifecycle")

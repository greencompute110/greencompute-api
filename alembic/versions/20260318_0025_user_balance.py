"""add user balance and quotas

Revision ID: 20260318_0025
Revises: 20260318_0024
Create Date: 2026-03-18 04:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260318_0025"
down_revision = "20260318_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("balance_tao", sa.Float(), nullable=False, server_default="0"))
    op.add_column("users", sa.Column("balance_usd", sa.Float(), nullable=False, server_default="0"))
    op.alter_column("users", "balance_tao", server_default=None)
    op.alter_column("users", "balance_usd", server_default=None)


def downgrade() -> None:
    op.drop_column("users", "balance_usd")
    op.drop_column("users", "balance_tao")

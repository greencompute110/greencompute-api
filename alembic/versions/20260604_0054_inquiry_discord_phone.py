"""commercial + bare-metal inquiries: add optional discord + phone columns

Sales asked to let prospects optionally attach a Discord handle or a phone
number for direct follow-up. Both are free-text (no server-side validation,
mirroring deployment_date/budget) and default to empty, so the columns are
additive and safe to backfill on existing rows.

Revision ID: 20260604_0054
Revises: 20260604_0053
Create Date: 2026-06-04
"""
from alembic import op
import sqlalchemy as sa


revision = "20260604_0054"
down_revision = "20260604_0053"
branch_labels = None
depends_on = None

_TABLES = ("commercial_inquiries", "bare_metal_inquiries")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table, sa.Column("discord", sa.String(64), nullable=False, server_default="")
        )
        op.add_column(
            table, sa.Column("phone", sa.String(32), nullable=False, server_default="")
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "phone")
        op.drop_column(table, "discord")

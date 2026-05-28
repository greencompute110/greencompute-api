"""key_value_state: small reusable kv store for cron-style watchers

Used by the on-chain deposit watcher to track `last_scanned_block` per
chain so we don't rescan from genesis on every tick.

Revision ID: 20260522_0045
Revises: 20260519_0044
Create Date: 2026-05-22
"""
from alembic import op
import sqlalchemy as sa


revision = "20260522_0045"
down_revision = "20260519_0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "key_value_state",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("value", sa.Text(), server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("key_value_state")

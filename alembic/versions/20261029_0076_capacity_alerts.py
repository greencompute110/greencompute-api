"""capacity_alerts ("tell me when this GPU is free")

One row per alert: fires once, when the pool has room for `count` rentals of
`gpu_count` GPUs of `gpu_model` (a capacity.available account event and an
email), or expires after 7 days.

Revision ID: 20261029_0076
Revises: 20261029_0075
Create Date: 2026-10-29
"""
import sqlalchemy as sa
from alembic import op

revision = "20261029_0076"
down_revision = "20261029_0075"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "capacity_alerts",
        sa.Column("alert_id", sa.String(length=64), primary_key=True),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("gpu_model", sa.String(length=64), nullable=False),
        sa.Column("gpu_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="waiting"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_capacity_alerts_user_id", "capacity_alerts", ["user_id"])
    op.create_index("ix_capacity_alerts_state", "capacity_alerts", ["state"])


def downgrade() -> None:
    op.drop_index("ix_capacity_alerts_state", table_name="capacity_alerts")
    op.drop_index("ix_capacity_alerts_user_id", table_name="capacity_alerts")
    op.drop_table("capacity_alerts")

"""account_events + webhooks

account_events: what a customer's code may act on (a deployment became ready,
failed, was suspended or ended; the balance runs low), read with
GET /platform/events and pushed to webhooks. Written in the same transaction
as the change. Kept 30 days.
webhooks: a customer's HTTPS endpoints for those events, with how far
delivery has got.

Revision ID: 20261029_0075
Revises: 20261029_0074
Create Date: 2026-10-29
"""
import sqlalchemy as sa
from alembic import op

revision = "20261029_0075"
down_revision = "20261029_0074"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "account_events",
        sa.Column("event_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("deployment_id", sa.String(length=64), nullable=True),
        sa.Column("data", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_account_events_user_event", "account_events", ["user_id", "event_id"])
    op.create_index("ix_account_events_created_at", "account_events", ["created_at"])
    op.create_table(
        "webhooks",
        sa.Column("webhook_id", sa.String(length=64), primary_key=True),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("secret", sa.String(length=128), nullable=False),
        sa.Column("events", sa.JSON(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("disabled_reason", sa.String(length=512), nullable=True),
        sa.Column("last_event_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failing_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_webhooks_user_id", "webhooks", ["user_id"])
    op.create_index("ix_webhooks_active", "webhooks", ["active"])


def downgrade() -> None:
    op.drop_index("ix_webhooks_active", table_name="webhooks")
    op.drop_index("ix_webhooks_user_id", table_name="webhooks")
    op.drop_table("webhooks")
    op.drop_index("ix_account_events_created_at", table_name="account_events")
    op.drop_index("ix_account_events_user_event", table_name="account_events")
    op.drop_table("account_events")

"""add workflow event outbox

Revision ID: 20260317_0002
Revises: 20260317_0001
Create Date: 2026-03-17 01:00:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_0002"
down_revision = "20260317_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workflow_events",
        sa.Column("event_id", sa.String(length=64), primary_key=True),
        sa.Column("subject", sa.String(length=128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_workflow_events_subject", "workflow_events", ["subject"])
    op.create_index("ix_workflow_events_status", "workflow_events", ["status"])
    op.create_index("ix_workflow_events_available_at", "workflow_events", ["available_at"])


def downgrade() -> None:
    op.drop_index("ix_workflow_events_available_at", table_name="workflow_events")
    op.drop_index("ix_workflow_events_status", table_name="workflow_events")
    op.drop_index("ix_workflow_events_subject", table_name="workflow_events")
    op.drop_table("workflow_events")

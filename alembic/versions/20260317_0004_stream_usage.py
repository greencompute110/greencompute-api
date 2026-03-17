"""add streaming usage fields

Revision ID: 20260317_0004
Revises: 20260317_0003
Create Date: 2026-03-17 15:05:00
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260317_0004"
down_revision = "20260317_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "usage_records",
        sa.Column("streamed_request_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "usage_records",
        sa.Column("stream_chunk_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("usage_records", "stream_chunk_count")
    op.drop_column("usage_records", "streamed_request_count")
